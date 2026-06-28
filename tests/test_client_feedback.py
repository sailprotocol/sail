"""
Client feedback / cancel (first-contact P2).

- A user CANCEL (generator close) stops the pay loop — no further chunk is paid — and leaves the
  host's reputation NEUTRAL (cancel is the user's choice, not the host's fault).
- run_inference emits incremental `progress` events (so the GUI can show movement, not dead air).
- A normal completion still records success (no regression from the cancel handling).

Run standalone (`python tests/test_client_feedback.py`) or under pytest.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.l402 import DONE_MARKER, NEXT_MARKER, done_trailer, next_trailer  # noqa: E402

_NEXT_CH = {"session_id": "s1", "macaroon": "m1", "invoice": "lnbc1",
            "payment_hash": "ph1", "amount_msat": 8000}


class _Host(BaseHTTPRequestHandler):
    mode = "loop"   # loop = always another chunk; done = one NEXT then DONE; single = DONE now
    count = 0

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        if not self.headers.get("Authorization"):  # session start -> 402
            b = json.dumps(_NEXT_CH).encode()
            self.send_response(402); self.send_header("content-length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        type(self).count += 1
        if self.mode == "single":
            body = "tok " + done_trailer(8000)
        elif self.mode == "done":
            body = "tok " + (next_trailer(_NEXT_CH) if self.count == 1 else done_trailer(16000))
        else:  # loop forever -> client would keep paying until cancelled
            body = "tok " + next_trailer(_NEXT_CH)
        body = body.encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextmanager
def _host(mode, port=8074):
    _Host.mode, _Host.count = mode, 0
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Host)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield types.SimpleNamespace(pubkey="cf" * 32, endpoint=f"http://127.0.0.1:{port}")
    finally:
        srv.shutdown()


@contextmanager
def _clean_rep():
    import client.core as core
    with tempfile.TemporaryDirectory() as d:
        saved = os.environ.get("REPUTATION_PATH")
        os.environ["REPUTATION_PATH"] = os.path.join(d, "rep.json")
        try:
            yield core
        finally:
            os.environ.pop("REPUTATION_PATH", None) if saved is None else os.environ.__setitem__("REPUTATION_PATH", saved)


def test_cancel_stops_payment_and_keeps_reputation_neutral():
    import client.reputation as reputation
    with _clean_rep() as core, _host("loop") as host:
        calls = []
        orig = core.pay_invoice
        core.pay_invoice = lambda *a, **k: (calls.append(1), "de" * 32)[1]
        try:
            g = core.run_inference(host, "hi", max_tokens=64)
            for ev in g:                 # advance to the first delivered token, then cancel
                if ev["type"] == "token":
                    break
            g.close()                    # simulate GUI Cancel / client disconnect
        finally:
            core.pay_invoice = orig
        assert calls == [1], f"cancel must stop further chunk payment, got {len(calls)} pays"
        assert reputation.load() == {}, "a user cancel must NOT penalize the host"


def test_progress_events_emitted():
    with _clean_rep() as core, _host("done") as host:
        orig = core.pay_invoice
        core.pay_invoice = lambda *a, **k: "de" * 32
        try:
            events = list(core.run_inference(host, "hi", max_tokens=64))
        finally:
            core.pay_invoice = orig
        assert any(e["type"] == "progress" and "spent_msat" in e for e in events), events
        assert any(e["type"] == "done" for e in events), events


def test_completion_still_records_success():
    import client.reputation as reputation
    with _clean_rep() as core, _host("single") as host:
        orig = core.pay_invoice
        core.pay_invoice = lambda *a, **k: "de" * 32
        try:
            events = list(core.run_inference(host, "hi", max_tokens=8))
        finally:
            core.pay_invoice = orig
        assert any(e["type"] == "done" for e in events), events
        assert reputation.load().get(host.pubkey, {}).get("successes") == 1, reputation.load()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[client-feedback] {len(fns)} tests PASS")
