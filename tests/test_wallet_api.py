"""
Host wallet — Stage 1 (read-only: balance + channels).

Covers the phoenixd wrapper (PhoenixdWallet) success + failure surfacing and the canReceive /
liquidity derivation, plus the daemon endpoints' local-only + phoenixd-only gates and their 502
surfacing when phoenixd fails. phoenixd itself is mocked — no node required.

Run standalone (`python tests/test_wallet_api.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

# hermetic import config (matches the other daemon tests)
os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local")
from host import wallet  # noqa: E402


# ---- fakes ----------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """Stand-in for httpx.Client: routes {path: FakeResp | Exception}."""
    base_url = "http://fake-phoenixd"

    def __init__(self, routes):
        self.routes = routes

    def get(self, path):
        return self._route(path)

    def post(self, path, data=None):
        self.last_post = {"path": path, "data": data}
        return self._route(path)

    def _route(self, path):
        r = self.routes[path]
        if isinstance(r, Exception):
            raise r
        return r


def _wallet(routes):
    return wallet.PhoenixdWallet(client=FakeClient(routes))


# ---- PhoenixdWallet.balance ----------------------------------------------
def test_balance_success():
    w = _wallet({"/getbalance": FakeResp(payload={"balanceSat": 42000, "feeCreditSat": 1500})})
    assert w.balance() == {"balanceSat": 42000, "feeCreditSat": 1500}


def test_balance_unreachable_raises_walleterror():
    w = _wallet({"/getbalance": httpx.ConnectError("refused")})
    try:
        w.balance()
    except wallet.WalletError as e:
        assert "unreachable" in str(e).lower(), e
    else:
        raise AssertionError("expected WalletError")


def test_balance_http_error_surfaces_status():
    w = _wallet({"/getbalance": FakeResp(status=401, text="unauthorized")})
    try:
        w.balance()
    except wallet.WalletError as e:
        assert "401" in str(e), e
    else:
        raise AssertionError("expected WalletError on non-200")


# ---- PhoenixdWallet.channels + canReceive ---------------------------------
def test_channels_canreceive_true_with_inbound():
    w = _wallet({"/listchannels": FakeResp(payload=[
        {"channelId": "abc", "type": "fr.acinq.lightning.channel.states.Normal",
         "balanceSat": 5000, "inboundLiquiditySat": 30000},
    ])})
    out = w.channels()
    assert out["count"] == 1
    assert out["canReceive"] is True
    assert out["inboundSat"] == 30000 and out["outboundSat"] == 5000
    assert out["channels"][0]["state"] == "Normal"  # last dotted component
    assert out["channels"][0]["channelId"] == "abc"


def test_channels_canreceive_false_without_inbound():
    w = _wallet({"/listchannels": FakeResp(payload=[
        {"channelId": "x", "type": "...states.Normal", "balanceSat": 9000, "inboundLiquiditySat": 0},
    ])})
    out = w.channels()
    assert out["canReceive"] is False and out["inboundSat"] == 0


def test_channels_empty_means_no_channel():
    w = _wallet({"/listchannels": FakeResp(payload=[])})
    out = w.channels()
    assert out == {"channels": [], "count": 0, "inboundSat": 0, "outboundSat": 0, "canReceive": False}


def test_channels_totals_sum_across_channels():
    w = _wallet({"/listchannels": FakeResp(payload=[
        {"channelId": "a", "type": "x.Normal", "balanceSat": 1000, "inboundLiquiditySat": 2000},
        {"channelId": "b", "type": "x.Normal", "balanceSat": 3000, "inboundLiquiditySat": 4000},
    ])})
    out = w.channels()
    assert out["outboundSat"] == 4000 and out["inboundSat"] == 6000 and out["count"] == 2


# ---- daemon endpoints: gates + surfacing ----------------------------------
def _client():
    from fastapi.testclient import TestClient
    import host.daemon as d
    return TestClient(d.app), d


def _set(monkeypatch, payments):
    monkeypatch.setenv("PAYMENTS", payments)


def test_endpoint_blocked_over_onion(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.get("/api/wallet/balance", headers={"host": "abcd.onion"})
    assert r.status_code == 404, r.text  # never exposed over the onion


def test_endpoint_requires_phoenixd_backend(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "lnd")  # wallet is phoenixd-specific
    r = c.get("/api/wallet/balance")
    assert r.status_code == 400, r.text
    assert "phoenixd" in r.json()["error"]


def test_endpoint_balance_ok(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/getbalance": FakeResp(payload={"balanceSat": 7, "feeCreditSat": 0})}), None))
    r = c.get("/api/wallet/balance")
    assert r.status_code == 200 and r.json()["balanceSat"] == 7, r.text


def test_endpoint_surfaces_phoenixd_failure_as_502(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/getbalance": httpx.ConnectError("refused")}), None))
    r = c.get("/api/wallet/balance")
    assert r.status_code == 502, r.text
    assert "unreachable" in r.json()["error"].lower()


def test_endpoint_channels_ok(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/listchannels": FakeResp(payload=[
                            {"channelId": "z", "type": "x.Normal", "balanceSat": 100, "inboundLiquiditySat": 200}])}), None))
    r = c.get("/api/wallet/channels")
    assert r.status_code == 200 and r.json()["canReceive"] is True, r.text


# ---- PhoenixdWallet.receive ----------------------------------------------
def test_receive_success_returns_bolt11():
    w = _wallet({"/createinvoice": FakeResp(payload={"serialized": "lnbc300u1pxyz", "paymentHash": "hh"})})
    out = w.receive(amount_sat=30000, description="fund")
    assert out == {"bolt11": "lnbc300u1pxyz", "paymentHash": "hh",
                   "amountSat": 30000, "description": "fund"}
    assert w._client.last_post["data"]["amountSat"] == "30000"  # forwarded to phoenixd as a string


def test_receive_no_amount_omits_amountsat():
    fc = FakeClient({"/createinvoice": FakeResp(payload={"serialized": "lnbc1", "paymentHash": "h"})})
    wallet.PhoenixdWallet(client=fc).receive(amount_sat=None)
    assert "amountSat" not in fc.last_post["data"]  # any-amount invoice


def test_receive_unreachable_raises():
    w = _wallet({"/createinvoice": httpx.ConnectError("refused")})
    try:
        w.receive(amount_sat=1000)
    except wallet.WalletError as e:
        assert "unreachable" in str(e).lower(), e
    else:
        raise AssertionError("expected WalletError")


# ---- receive endpoint: success (bolt11 + QR), validation, gates, surfacing -
def test_endpoint_receive_success_has_bolt11_and_qr(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/createinvoice": FakeResp(payload={"serialized": "lnbcQR", "paymentHash": "p"})}), None))
    r = c.post("/api/wallet/receive", json={"amountSat": 30000})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["bolt11"] == "lnbcQR"
    assert j["qr"].startswith("data:image/svg"), j["qr"][:30]  # QR rendered locally via segno


def test_endpoint_receive_rejects_bad_amount(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    for bad in ("abc", 0, -5):
        r = c.post("/api/wallet/receive", json={"amountSat": bad})
        assert r.status_code == 400, (bad, r.text)


def test_endpoint_receive_blocked_over_onion(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/receive", json={}, headers={"host": "zzz.onion"})
    assert r.status_code == 404, r.text


def test_endpoint_receive_requires_phoenixd(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "nwc")
    r = c.post("/api/wallet/receive", json={})
    assert r.status_code == 400 and "phoenixd" in r.json()["error"], r.text


def test_endpoint_receive_surfaces_failure_as_502(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/createinvoice": httpx.ConnectError("refused")}), None))
    r = c.post("/api/wallet/receive", json={"amountSat": 1000})
    assert r.status_code == 502 and "unreachable" in r.json()["error"].lower(), r.text


if __name__ == "__main__":
    simple = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v) and "monkeypatch" not in v.__code__.co_varnames]
    for fn in simple:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[wallet-api] {len(simple)} tests PASS "
          f"(run `pytest tests/test_wallet_api.py` for the endpoint/gate tests too)")
