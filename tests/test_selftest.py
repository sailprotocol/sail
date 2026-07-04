"""
Self-test classification — a fresh/unfunded host must NOT read as a red failure.

/api/selftest checks only that a client can reach us and get a 402 + invoice. That is
FUNDING-INDEPENDENT (no channel, no payment round-trip). So the wizard must distinguish:
  - ok          -> green "402 passing"
  - unreachable -> amber "onion still propagating" (transient; the fresh onion, over Tor)
  - bad_status  -> red "failed" (we were reached but didn't answer 402 — a genuine problem)

This pins the `category` the UI branches on. phoenixd/model are mocked — no node required.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local", SAIL_OPERATOR_AUTOSTART="0")
import httpx  # noqa: E402
import host.daemon as d  # noqa: E402


def _client():
    from fastapi.testclient import TestClient
    return TestClient(d.operator_app)  # selftest lives on the localhost-only operator app


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


def test_selftest_ok_when_402_and_invoice(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(402, '{"invoice":"lnbc1..."}'))
    j = _client().get("/api/selftest").json()
    assert j["ok"] is True and j["category"] == "ok"


def test_selftest_bad_status_is_red_not_unreachable(monkeypatch):
    # reached us but answered wrong (e.g. 500/200) — a genuine serving problem, stays red
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(500, "boom"))
    j = _client().get("/api/selftest").json()
    assert j["ok"] is False and j["category"] == "bad_status" and j["status"] == 500


def test_selftest_unreachable_is_amber_transient(monkeypatch):
    # couldn't reach our own onion — over Tor this is the fresh onion still propagating (transient),
    # NOT a serving failure; the UI shows amber + retry, never a red FAILED.
    def _boom(*a, **k):
        raise httpx.ConnectError("onion not reachable yet")
    monkeypatch.setattr(httpx, "post", _boom)
    j = _client().get("/api/selftest").json()
    assert j["ok"] is False and j["category"] == "unreachable"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
