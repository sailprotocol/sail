"""
Client reputation re-bury fix (first-contact P1).

Acceptance:
- a single failure does NOT hide a host;
- a CLIENT-side payment failure does NOT penalize the host at all;
- N consecutive HOST failures hide it;
- the penalty decays — the host re-surfaces on its own after the cooldown (no manual reset).

Run standalone (`python tests/test_reputation.py`) or under pytest.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.l402 import error_trailer  # noqa: E402


@contextmanager
def _rep(**env):
    """Fresh reputation store + env (cooldown etc.), restored afterwards."""
    import client.reputation as r
    with tempfile.TemporaryDirectory() as d:
        saved = {k: os.environ.get(k) for k in (*env, "REPUTATION_PATH")}
        try:
            os.environ["REPUTATION_PATH"] = os.path.join(d, "rep.json")
            for k, v in env.items():
                os.environ[k] = v
            yield r
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _hosts(*pubkeys):
    return [types.SimpleNamespace(pubkey=pk) for pk in pubkeys]


def test_single_failure_does_not_hide():
    with _rep() as r:
        r.record("h1", success=False)
        kept, hidden = r.partition(_hosts("h1"), r.load())
        assert [h.pubkey for h in kept] == ["h1"] and hidden == [], (kept, hidden)


def test_two_failures_still_not_hidden():
    with _rep() as r:
        r.record("h1", success=False)
        r.record("h1", success=False)
        _, hidden = r.partition(_hosts("h1"), r.load())
        assert hidden == [], hidden  # below REP_HIDE_CONSEC (3)


def test_three_consecutive_failures_hide():
    with _rep() as r:
        for _ in range(3):
            r.record("h1", success=False)
        kept, hidden = r.partition(_hosts("h1"), r.load())
        assert [h.pubkey for h in hidden] == ["h1"] and kept == [], (kept, hidden)


def test_success_resets_consecutive():
    with _rep() as r:
        for _ in range(3):
            r.record("h1", success=False)
        r.record("h1", success=True, latency_ms=100)
        _, hidden = r.partition(_hosts("h1"), r.load())
        assert hidden == [], "a success should clear the consecutive-failure hide"


def test_penalty_decays_after_cooldown():
    with _rep(REP_COOLDOWN_SECONDS="600") as r:
        for _ in range(3):
            r.record("h1", success=False)
        rep = r.load()
        assert r._is_bad(rep["h1"]), "hidden within cooldown"
        rep["h1"]["last_fail_ts"] = int(time.time()) - 700  # age it past the 600s cooldown
        assert not r._is_bad(rep["h1"]), "must re-surface on its own after cooldown"
        info = r.hidden_reason(r.load()["h1"])
        assert info["consecutive"] == 3 and info["clears_in_s"] >= 0


def test_reset_clears_store():
    with _rep() as r:
        r.record("h1", success=False)
        assert r.reset() is True and r.load() == {}


# --- run_inference fault classification: payment != host fault ---
class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(n)
        if self.headers.get("Authorization"):  # paid re-request -> host serve failure
            body = ("partial " + error_trailer("host failed to serve", spent_msat=8000,
                                                delivered_tokens=1, reason="model_read")).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        else:
            b = json.dumps({"session_id": "s1", "macaroon": "m1", "invoice": "lnbc1",
                            "payment_hash": "ph1", "amount_msat": 8000}).encode()
            self.send_response(402); self.send_header("content-length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass


@contextmanager
def _stub_host(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield types.SimpleNamespace(pubkey="hp" * 32, endpoint=f"http://127.0.0.1:{port}")
    finally:
        srv.shutdown()


def test_payment_failure_does_not_penalize_host():
    """The dead-NWC-wallet case: payment raises -> host reputation UNTOUCHED."""
    import client.core as core
    with _rep() as r, _stub_host(8076) as host:
        orig = core.pay_invoice
        core.pay_invoice = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("NWC payment failed — dead wallet"))
        try:
            events = list(core.run_inference(host, "hi", max_tokens=8))
        finally:
            core.pay_invoice = orig
        assert any(e.get("kind") == "payment_failed" for e in events), events
        assert r.load() == {}, "host must NOT be recorded for a client payment failure"


def test_serve_failure_does_penalize_host():
    """A genuine host serve failure IS recorded (so repeated ones eventually hide)."""
    import client.core as core
    with _rep() as r, _stub_host(8076) as host:
        orig = core.pay_invoice
        core.pay_invoice = lambda *a, **k: "de" * 32   # payment succeeds; host then serve-fails
        try:
            events = list(core.run_inference(host, "hi", max_tokens=8))
        finally:
            core.pay_invoice = orig
        assert any(e.get("kind") == "serve_failed" for e in events), events
        assert r.load().get(host.pubkey, {}).get("failures") == 1, r.load()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[reputation] {len(fns)} tests PASS")
