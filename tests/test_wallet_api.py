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
def _getinfo(channels):
    # channels() reads /getinfo (the clean ApiType.Channel view), NOT /listchannels (raw objects)
    return {"/getinfo": FakeResp(payload={"nodeId": "n", "channels": channels})}


def test_channels_maps_balance_to_outbound_inbound():
    # balanceSat = availableBalanceForSend (outbound/can-send); inboundLiquiditySat = can-receive
    w = _wallet(_getinfo([
        {"channelId": "abc", "state": "Normal", "balanceSat": 6261,
         "inboundLiquiditySat": 30000, "capacitySat": 40000},
    ]))
    out = w.channels()
    assert out["count"] == 1 and out["normalCount"] == 1 and out["hasChannel"] is True
    assert out["outboundSat"] == 6261 and out["inboundSat"] == 30000  # consistent w/ spendable
    assert out["canReceive"] is True
    assert out["channels"][0]["state"] == "Normal" and out["channels"][0]["channelId"] == "abc"


def test_channels_open_but_no_inbound_haschannel_true_canreceive_false():
    # the reported bug scenario: spendable balance present, 0 inbound — a channel DOES exist
    w = _wallet(_getinfo([
        {"channelId": "x", "state": "Normal", "balanceSat": 6261, "inboundLiquiditySat": 0},
    ]))
    out = w.channels()
    assert out["hasChannel"] is True, "an open channel must register as existing"
    assert out["canReceive"] is False and out["inboundSat"] == 0
    assert out["outboundSat"] == 6261  # NOT 0 — the mapping bug is fixed


def test_channels_empty_means_no_channel():
    out = _wallet(_getinfo([])).channels()
    assert out["count"] == 0 and out["hasChannel"] is False and out["canReceive"] is False
    assert out["inboundSat"] == 0 and out["outboundSat"] == 0


def test_channels_non_normal_state_is_not_a_usable_channel():
    # e.g. an offline/opening channel exists in the list but isn't usable -> hasChannel False
    out = _wallet(_getinfo([{"channelId": "y", "state": "Offline", "balanceSat": 0}])).channels()
    assert out["count"] == 1 and out["normalCount"] == 0 and out["hasChannel"] is False


def test_channels_totals_sum_across_channels():
    out = _wallet(_getinfo([
        {"channelId": "a", "state": "Normal", "balanceSat": 1000, "inboundLiquiditySat": 2000},
        {"channelId": "b", "state": "Normal", "balanceSat": 3000, "inboundLiquiditySat": 4000},
    ])).channels()
    assert out["outboundSat"] == 4000 and out["inboundSat"] == 6000 and out["count"] == 2


# ---- daemon endpoints: gates + surfacing ----------------------------------
def _client():
    from fastapi.testclient import TestClient
    import host.daemon as d
    # wallet routes live on the LOCALHOST-only operator app, not the onion-exposed `app`
    return TestClient(d.operator_app), d


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
                        lambda req: (_wallet(_getinfo([
                            {"channelId": "z", "state": "Normal", "balanceSat": 100, "inboundLiquiditySat": 200}])), None))
    r = c.get("/api/wallet/channels")
    j = r.json()
    assert r.status_code == 200 and j["canReceive"] is True and j["hasChannel"] is True, r.text


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


# ---- PhoenixdWallet.pay / close / incoming_status -------------------------
def test_pay_success_returns_sent_and_fee():
    w = _wallet({"/payinvoice": FakeResp(payload={"paymentHash": "h", "recipientAmountSat": 5000, "routingFeeSat": 3})})
    out = w.pay("lnbc50u1pabc")
    assert out == {"paymentHash": "h", "recipientAmountSat": 5000, "routingFeeSat": 3}


def test_pay_empty_invoice_raises():
    w = _wallet({})
    try:
        w.pay("   ")
    except wallet.WalletError:
        pass
    else:
        raise AssertionError("expected WalletError for empty invoice")


def test_pay_failure_surfaces():
    w = _wallet({"/payinvoice": FakeResp(status=400, text="insufficient balance")})
    try:
        w.pay("lnbc1")
    except wallet.WalletError as e:
        assert "insufficient balance" in str(e), e
    else:
        raise AssertionError("expected WalletError")


def test_close_success_json_txid():
    w = _wallet({"/closechannel": FakeResp(payload={"txId": "deadbeef"})})
    assert w.close("c1", "bc1xyz", 2)["closingTxId"] == "deadbeef"


def test_close_success_plain_text_txid():
    # some phoenixd builds return the txid as plain text, not JSON
    w = _wallet({"/closechannel": FakeResp(status=200, payload=None, text="abc123txid")})
    assert w.close("c1", "bc1xyz", 2)["closingTxId"] == "abc123txid"


def test_incoming_status_paid_and_unpaid():
    paid = _wallet({"/payments/incoming/HH": FakeResp(payload={"isPaid": True, "receivedSat": 30000})})
    assert paid.incoming_status("HH") == {"paid": True, "receivedSat": 30000}
    unseen = _wallet({"/payments/incoming/HH": FakeResp(status=404, text="not found")})
    assert unseen.incoming_status("HH") == {"paid": False, "receivedSat": 0}


# ---- seed endpoint (security-critical) ------------------------------------
def test_seed_blocked_over_onion(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/seed", json={"confirm": "reveal"}, headers={"host": "secret.onion"})
    assert r.status_code == 404, r.text  # the seed must NEVER be reachable over the onion


def test_seed_requires_phoenixd(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "lnd")
    r = c.post("/api/wallet/seed", json={"confirm": "reveal"})
    assert r.status_code == 400, r.text


def test_seed_requires_explicit_confirm(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/seed", json={})
    assert r.status_code == 400 and "confirm" in r.json()["error"].lower(), r.text


def test_seed_success_returns_words(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    from host import phoenixd_setup
    words = ["abandon"] * 12
    monkeypatch.setattr(phoenixd_setup, "read_seed_words", lambda: words)
    r = c.post("/api/wallet/seed", json={"confirm": "reveal"})
    assert r.status_code == 200 and r.json() == {"words": words, "count": 12}, r.text


def test_seed_missing_file_is_404(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    from host import phoenixd_setup

    def _missing():
        raise FileNotFoundError("seed.dat not found")
    monkeypatch.setattr(phoenixd_setup, "read_seed_words", _missing)
    r = c.post("/api/wallet/seed", json={"confirm": "reveal"})
    assert r.status_code == 404, r.text


# ---- pay endpoint ---------------------------------------------------------
def test_endpoint_pay_success(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/payinvoice": FakeResp(payload={"paymentHash": "h", "recipientAmountSat": 9, "routingFeeSat": 0})}), None))
    r = c.post("/api/wallet/pay", json={"invoice": "lnbc1"})
    assert r.status_code == 200 and r.json()["recipientAmountSat"] == 9, r.text


def test_endpoint_pay_requires_invoice(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/pay", json={})
    assert r.status_code == 400, r.text


def test_endpoint_pay_blocked_over_onion(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/pay", json={"invoice": "lnbc1"}, headers={"host": "x.onion"})
    assert r.status_code == 404, r.text


def test_endpoint_pay_surfaces_failure_502(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/payinvoice": FakeResp(status=400, text="route not found")}), None))
    r = c.post("/api/wallet/pay", json={"invoice": "lnbc1"})
    assert r.status_code == 502 and "route not found" in r.json()["error"], r.text


# ---- close endpoint -------------------------------------------------------
def _close_wallet():
    # the close endpoint derives channel ids via channels() (-> /getinfo), then closes each
    return _wallet({
        "/getinfo": FakeResp(payload={"channels": [
            {"channelId": "c1", "state": "Normal", "balanceSat": 1000, "inboundLiquiditySat": 0}]}),
        "/closechannel": FakeResp(payload={"txId": "sweeptx"}),
    })


def test_endpoint_close_success(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error", lambda req: (_close_wallet(), None))
    r = c.post("/api/wallet/close", json={"address": "bc1qxyz", "feerateSatByte": 2})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["count"] == 1 and j["closed"][0]["closingTxId"] == "sweeptx", j


def test_endpoint_close_requires_address(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/close", json={"feerateSatByte": 2})
    assert r.status_code == 400, r.text


def test_endpoint_close_requires_feerate(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/close", json={"address": "bc1qxyz"})
    assert r.status_code == 400, r.text


def test_endpoint_close_no_channel_is_400(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet(_getinfo([])), None))
    r = c.post("/api/wallet/close", json={"address": "bc1qxyz", "feerateSatByte": 2})
    assert r.status_code == 400 and "no channel" in r.json()["error"].lower(), r.text


def test_endpoint_close_blocked_over_onion(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/close", json={"address": "bc1qxyz", "feerateSatByte": 2},
               headers={"host": "x.onion"})
    assert r.status_code == 404, r.text


# ---- incoming endpoint (receive-modal confirmation) -----------------------
def test_endpoint_incoming_reports_paid(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet({"/payments/incoming/HH": FakeResp(payload={"isPaid": True, "receivedSat": 30000})}), None))
    r = c.get("/api/wallet/incoming/HH")
    assert r.status_code == 200 and r.json() == {"paid": True, "receivedSat": 30000}, r.text


# ---- setup payout: import-existing-seed branch ----------------------------
_VALID12 = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def _setup_client():
    from fastapi.testclient import TestClient
    import host.daemon as d
    return TestClient(d.operator_app), d  # /api/setup/* is on the operator app


def test_payout_import_rejects_invalid_seed(monkeypatch):
    c, _ = _setup_client()
    monkeypatch.setenv("PAYMENTS", "mock")
    r = c.post("/api/setup/payout", json={"tier": "phoenixd", "mode": "import", "seed": "not a real seed"})
    assert r.status_code == 400, r.text  # BIP39 validation fails before touching anything


def test_payout_import_success_returns_imported_no_seed_ceremony(monkeypatch):
    c, d = _setup_client()
    monkeypatch.setenv("PAYMENTS", "mock")
    from host import config_writer, phoenixd_setup
    monkeypatch.setattr(config_writer, "update_env_file", lambda *a, **k: None)
    monkeypatch.setattr(phoenixd_setup, "is_provisioned", lambda: False)  # fresh host, no wallet yet
    monkeypatch.setattr(phoenixd_setup, "import_seed",
                        lambda words, replace=False: {"imported": True, "service": {"installed": False}})
    r = c.post("/api/setup/payout", json={"tier": "phoenixd", "mode": "import", "seed": _VALID12})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] is True and body["tier"] == "phoenixd"
    assert "seed_words" not in body  # imported wallet: operator already holds the phrase, no ceremony


def test_payout_import_refuses_funded_existing_wallet(monkeypatch):
    c, d = _setup_client()
    monkeypatch.setenv("PAYMENTS", "mock")
    from host import phoenixd_setup
    monkeypatch.setattr(phoenixd_setup, "is_provisioned", lambda: True)
    monkeypatch.setattr(phoenixd_setup, "wallet_funded_status", lambda: (True, "balance 5000 sat"))
    r = c.post("/api/setup/payout", json={"tier": "phoenixd", "mode": "import", "seed": _VALID12})
    assert r.status_code == 409 and r.json().get("funded") is True, r.text  # protect real funds


def test_payout_import_asks_replace_for_empty_existing_wallet(monkeypatch):
    c, d = _setup_client()
    monkeypatch.setenv("PAYMENTS", "mock")
    from host import phoenixd_setup
    monkeypatch.setattr(phoenixd_setup, "is_provisioned", lambda: True)
    monkeypatch.setattr(phoenixd_setup, "wallet_funded_status", lambda: (False, "balance 0 sat, 0 channel(s)"))
    r = c.post("/api/setup/payout", json={"tier": "phoenixd", "mode": "import", "seed": _VALID12})
    assert r.status_code == 409 and r.json().get("needs_replace") is True, r.text  # not a dead-end


def test_payout_import_replace_proceeds(monkeypatch):
    c, d = _setup_client()
    monkeypatch.setenv("PAYMENTS", "mock")
    from host import config_writer, phoenixd_setup
    monkeypatch.setattr(config_writer, "update_env_file", lambda *a, **k: None)
    monkeypatch.setattr(phoenixd_setup, "is_provisioned", lambda: True)  # exists, but replace given
    seen = {}
    monkeypatch.setattr(phoenixd_setup, "import_seed",
                        lambda words, replace=False: seen.update(replace=replace) or {"imported": True, "service": {}})
    r = c.post("/api/setup/payout", json={"tier": "phoenixd", "mode": "import", "seed": _VALID12, "replace": True})
    assert r.status_code == 200 and r.json()["imported"] is True, r.text
    assert seen["replace"] is True  # replace flag threaded through


def test_payout_generate_still_returns_seed_for_ceremony(monkeypatch):
    c, d = _setup_client()
    monkeypatch.setenv("PAYMENTS", "mock")
    from host import config_writer, phoenixd_setup
    seed = ["abandon"] * 12
    monkeypatch.setattr(config_writer, "update_env_file", lambda *a, **k: None)
    monkeypatch.setattr(phoenixd_setup, "provision",
                        lambda: {"seed_words": seed, "service": {"installed": False}})
    r = c.post("/api/setup/payout", json={"tier": "phoenixd", "mode": "generate"})
    assert r.status_code == 200 and r.json()["seed_words"] == seed, r.text


# ---- physical separation: onion app must NOT mount operator routes --------
def _paths(app):
    return {getattr(r, "path", None) for r in app.routes}


def test_public_app_excludes_wallet_and_setup_routes():
    """Regression guard: the onion-exposed `app` must expose ONLY public routes. If a wallet/setup/
    control route ever lands on it, it'd be reachable over Tor — fail loudly here."""
    import host.daemon as d
    public = _paths(d.app)
    leaked = [p for p in public if p and (p.startswith("/api/wallet") or p.startswith("/api/setup")
                                          or p.startswith("/api/control") or p in ("/", "/setup"))]
    assert leaked == [], f"operator routes leaked onto the onion-exposed app: {leaked}"
    assert "/v1/inference" in public, "public inference endpoint must stay on the onion app"


def test_operator_app_has_wallet_and_setup_but_not_inference():
    import host.daemon as d
    op = _paths(d.operator_app)
    assert "/api/wallet/seed" in op and "/api/wallet/pay" in op and "/api/wallet/close" in op
    assert "/api/setup/payout" in op and "/" in op and "/setup" in op
    assert "/v1/inference" not in op  # inference is not served on the operator port


# ---- startup backstop: tighten seed perms on a phoenixd host --------------
def _arm_startup(monkeypatch, d):
    """Neutralize publish_listing's other side effects so we can assert just the perms backstop."""
    monkeypatch.setattr(d, "_maybe_start_operator", lambda: None)
    monkeypatch.setattr(d.moderation, "assert_can_serve", lambda m: None)
    monkeypatch.setattr(d.registry, "publish", lambda listing: {"success": ["x"], "failed": {}})
    monkeypatch.setattr(d, "TRANSPORT", "clearnet")
    monkeypatch.setattr(d, "REANNOUNCE_SECONDS", 0)
    monkeypatch.setenv("REGISTRY", "local")


def test_startup_tightens_seed_perms_on_phoenixd_host(monkeypatch):
    import host.daemon as d
    from host import phoenixd_setup
    called = []
    monkeypatch.setattr(phoenixd_setup, "secure_seed_files", lambda: called.append(True) or {})
    _arm_startup(monkeypatch, d)
    monkeypatch.setenv("PAYMENTS", "phoenixd")
    d.publish_listing()
    assert called == [True], "phoenixd host must tighten ~/.phoenix perms on startup"


def test_startup_skips_seed_perms_on_non_phoenixd_host(monkeypatch):
    import host.daemon as d
    from host import phoenixd_setup
    called = []
    monkeypatch.setattr(phoenixd_setup, "secure_seed_files", lambda: called.append(True) or {})
    _arm_startup(monkeypatch, d)
    monkeypatch.setenv("PAYMENTS", "mock")
    d.publish_listing()
    assert called == [], "non-phoenixd host has no phoenixd seed to tighten"


# ---- close fee/net estimate + dust guard ----------------------------------
def test_close_quote_normal_balance():
    q = wallet.estimate_close_quote(100_000, 2)
    assert q["feeSat"] == 2 * wallet.CLOSE_TX_VSIZE and q["netSat"] == 100_000 - q["feeSat"]
    assert q["dust"] is False and q["estimate"] is True


def test_close_quote_dust_when_balance_below_fee():
    q = wallet.estimate_close_quote(300, 2)  # fee 400 > balance 300
    assert q["dust"] is True and q["netSat"] == 0
    assert "at or below the on-chain close fee" in q["detail"]


def test_close_quote_dust_when_net_near_dust_limit():
    q = wallet.estimate_close_quote(800, 2)  # fee 400, net 400 <= 546 dust limit
    assert q["dust"] is True and q["netSat"] == 400
    assert "dust" in q["detail"].lower()


# ---- decode-invoice endpoint ----------------------------------------------
def test_endpoint_decode_fixed_amount(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/decode-invoice", json={"invoice": "lnbc2500u1" + "q" * 10})
    assert r.status_code == 200 and r.json() == {"amountSat": 250_000, "fixed": True}, r.text


def test_endpoint_decode_any_amount(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.post("/api/wallet/decode-invoice", json={"invoice": "lnbc1" + "q" * 10})
    assert r.status_code == 200 and r.json() == {"amountSat": None, "fixed": False}, r.text


def test_endpoint_decode_requires_phoenixd(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "lnd")
    r = c.post("/api/wallet/decode-invoice", json={"invoice": "lnbc2500u1xx"})
    assert r.status_code == 400, r.text


# ---- close-quote endpoint -------------------------------------------------
def test_endpoint_close_quote_ok(monkeypatch):
    c, d = _client()
    _set(monkeypatch, "phoenixd")
    monkeypatch.setattr(d, "_wallet_or_error",
                        lambda req: (_wallet(_getinfo([
                            {"channelId": "c", "state": "Normal", "balanceSat": 100_000, "inboundLiquiditySat": 0}])), None))
    r = c.get("/api/wallet/close-quote?feerate=2")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["balanceSat"] == 100_000 and j["feeSat"] == 2 * wallet.CLOSE_TX_VSIZE and j["dust"] is False


def test_endpoint_close_quote_rejects_bad_feerate(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    for bad in ("0", "abc", "-1", ""):
        r = c.get("/api/wallet/close-quote?feerate=" + bad)
        assert r.status_code == 400, (bad, r.text)


def test_endpoint_close_quote_blocked_over_onion(monkeypatch):
    c, _ = _client()
    _set(monkeypatch, "phoenixd")
    r = c.get("/api/wallet/close-quote?feerate=2", headers={"host": "x.onion"})
    assert r.status_code == 404, r.text


if __name__ == "__main__":
    simple = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v) and "monkeypatch" not in v.__code__.co_varnames]
    for fn in simple:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[wallet-api] {len(simple)} tests PASS "
          f"(run `pytest tests/test_wallet_api.py` for the endpoint/gate tests too)")
