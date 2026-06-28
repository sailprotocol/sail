"""
phoenixd channel-cliff guard (first-contact punch-list P3).

A brand-new phoenixd node can't RECEIVE until an inbound channel exists: the first inbound payment
is held as feeCreditSat with NO channel until ~25-35k sat opens one. go-live / the dashboard must not
claim a host is "live to earn" while it silently can't be paid. This checks:

  - base LightningBackend / mock are always receivable (no regression for lnd/mock/nwc),
  - phoenixd with no channel -> receivable False + a bootstrap-path detail,
  - phoenixd with a "Normal" channel (like host #2) -> receivable True (unaffected),
  - phoenixd unreachable -> receivable None (unknown, not a false "can't receive"),
  - /api/status carries receivable + receive_detail end-to-end.

Run standalone (`python tests/test_channel_cliff.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local",
                  HOST_KEY_PATH="/tmp/sail-cliff-host.nsec",
                  LISTING_REANNOUNCE_SECONDS="0")
os.environ.pop("MODEL_ALLOWLIST", None)
os.environ.pop("MODEL_ALLOWLIST_PATH", None)

import httpx  # noqa: E402

from host.payments import LightningBackend, MockLightning, PhoenixdLightning  # noqa: E402


class _Resp:
    def __init__(self, payload, status_code=200, raise_err=None):
        self._payload = payload
        self.status_code = status_code
        self._raise_err = raise_err

    def raise_for_status(self):
        if self._raise_err is not None:
            raise self._raise_err

    def json(self):
        return self._payload


class _FakeClient:
    """Routes GET by path to canned responses, mimicking phoenixd's local HTTP API."""
    base_url = "http://127.0.0.1:9740"

    def __init__(self, routes):
        self._routes = routes

    def get(self, path, *a, **k):
        v = self._routes[path]
        if isinstance(v, Exception):
            raise v
        return v


def _phoenixd_with(routes) -> PhoenixdLightning:
    p = object.__new__(PhoenixdLightning)  # skip __init__ (needs real env + httpx.Client)
    p._client = _FakeClient(routes)
    return p


def test_base_and_mock_are_always_receivable():
    base = LightningBackend().receive_status()
    assert base["receivable"] is True, base
    mock = MockLightning().receive_status()
    assert mock["receivable"] is True, mock


def test_phoenixd_no_channel_is_not_receivable_with_bootstrap_path():
    # listchannels empty, balance only as fee credit -> the cliff
    p = _phoenixd_with({
        "/listchannels": _Resp([]),
        "/getbalance": _Resp({"balanceSat": 0, "feeCreditSat": 12000}),
    })
    st = p.receive_status()
    assert st["receivable"] is False, st
    assert st["channels"] == 0, st
    assert st["fee_credit_sat"] == 12000, st
    d = st["detail"].lower()
    assert "no inbound channel" in d, st
    assert "25-35k sat" in d, st          # the bootstrap amount must be surfaced
    assert "fee credit" in d, st          # held credit explained


def test_phoenixd_normal_channel_is_receivable():
    # host #2's healthy state: an open, usable channel -> live, unaffected
    p = _phoenixd_with({
        "/listchannels": _Resp([{"state": "Normal", "channelId": "abc"}]),
        "/getbalance": _Resp({"balanceSat": 40000, "feeCreditSat": 0}),
    })
    st = p.receive_status()
    assert st["receivable"] is True, st
    assert st["channels"] == 1, st
    assert "receivable" in st["detail"].lower(), st


def test_phoenixd_channel_present_but_not_normal_is_not_receivable():
    # opening/offline channel -> present but not usable yet
    p = _phoenixd_with({
        "/listchannels": _Resp([{"state": "Opening"}]),
        "/getbalance": _Resp({"balanceSat": 0, "feeCreditSat": 0}),
    })
    st = p.receive_status()
    assert st["receivable"] is False, st
    assert "not usable" in st["detail"].lower(), st


def test_phoenixd_unreachable_is_unknown_not_false():
    p = _phoenixd_with({"/listchannels": httpx.ConnectError("refused")})
    st = p.receive_status()
    assert st["receivable"] is None, st          # unknown, NOT a false "can't receive"
    assert "unreachable" in st["detail"].lower(), st


def test_api_status_carries_receivable():
    from fastapi.testclient import TestClient
    import host.daemon as d
    # mock backend -> always receivable; status must still expose the fields
    d._pay_health = {"ts": 0.0, "ok": None, "detail": "", "receivable": True, "receive_detail": "ok"}
    c = TestClient(d.app)
    j = c.get("/api/status").json()
    assert "receivable" in j, j
    assert "receive_detail" in j, j
    assert j["receivable"] is True, j


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[channel-cliff] {len(fns)} tests PASS")
