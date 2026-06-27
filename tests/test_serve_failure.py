"""
Pay-then-fail serving semantics (infra punch-list P3).

When the model dies mid-stream AFTER a chunk was paid, the host must end the stream CLEANLY with a
typed error trailer (not a silent cut), charge nothing beyond the in-flight chunk, and the client
must surface "host failed to serve — N sat spent" with the partial output. Also checks the host
logs WHERE it aborted (phase), for diagnosis.

Run standalone (`python tests/test_serve_failure.py`) or under pytest.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Hermetic env BEFORE importing the daemon (module-level identity/config).
os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local",
                  HOST_KEY_PATH="/tmp/sail-servefail-host.nsec",
                  LISTING_REANNOUNCE_SECONDS="0")
os.environ.pop("MODEL_ALLOWLIST", None)
os.environ.pop("MODEL_ALLOWLIST_PATH", None)

from shared.l402 import ERROR_MARKER, NEXT_MARKER, error_trailer  # noqa: E402


class _FailingModel:
    """Yields a few tokens, then dies like Ollama dropping mid-stream (OOM / connection reset)."""
    name = "fail-model:1b"
    modality = "text"

    def stream(self, prompt):
        for i in range(3):
            yield f"t{i} "
        raise RuntimeError("ollama connection died (simulated)")


def test_host_emits_error_trailer_and_charges_only_inflight_chunk():
    from fastapi.testclient import TestClient
    import host.daemon as d

    d._model = _FailingModel()
    c = TestClient(d.app)

    r = c.post("/v1/inference", json={"prompt": "hi", "max_tokens": 24})
    assert r.status_code == 402, r.status_code
    ch = r.json()
    pre = c.post("/mock/pay", json={"payment_hash": ch["payment_hash"]}).json()["preimage"]

    log = io.StringIO()
    with redirect_stdout(log):
        r2 = c.post("/v1/inference", json={"session_id": ch["session_id"]},
                    headers={"Authorization": f"L402 {ch['macaroon']}:{pre}"})

    body = r2.text
    assert r2.status_code == 200, "stream must end cleanly (200), not crash"
    assert "t0 t1 t2 " in body, "partial tokens must be delivered before the error"
    assert ERROR_MARKER in body, "must end with a typed error trailer, not a silent cut"
    assert NEXT_MARKER not in body, "must NOT charge a further chunk after failing"
    info = json.loads(body.split(ERROR_MARKER, 1)[1])
    assert info["delivered_tokens"] == 3, info
    assert info["spent_msat"] == ch["amount_msat"], "charged exactly the one in-flight chunk"
    assert "model_read" in info["reason"], info["reason"]
    # host logged WHERE it aborted, for diagnosis
    assert "serve ABORTED" in log.getvalue() and "phase=model_read" in log.getvalue()
    # session cleaned up
    assert ch["session_id"] not in d._sessions


# --- client side: parse the error trailer into a serve_failed event ---
class _StubHost(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(n)
        if self.path == "/mock/pay":
            return self._json(200, {"preimage": "de" * 32})
        if self.path == "/v1/inference":
            if self.headers.get("Authorization"):  # paid request -> tokens then a clean error end
                body = ("hello world " + error_trailer(
                    "host failed to serve the model (stream ended early)",
                    spent_msat=8000, delivered_tokens=2, reason="model_read:RuntimeError")).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            else:  # first request -> 402 challenge
                self._json(402, {"session_id": "s1", "macaroon": "m1", "invoice": "lnbc1",
                                 "payment_hash": "ph1", "amount_msat": 8000})
            return
        self.send_response(404); self.end_headers()

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def test_client_surfaces_serve_failed_and_penalizes_host():
    from shared.listing import HostListing, ModelOffer
    srv = ThreadingHTTPServer(("127.0.0.1", 8077), _StubHost)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as dtmp:
            os.environ["REPUTATION_PATH"] = os.path.join(dtmp, "rep.json")
            os.environ["PAYMENTS"] = "mock"
            from client import core, reputation
            host = HostListing(pubkey="ab" * 32, endpoint="http://127.0.0.1:8077",
                               models=[ModelOffer(name="fail-model:1b", price_msat_per_token=1000,
                                                  context_window=8192)])
            events = list(core.run_inference(host, "hi", max_tokens=24))
            kinds = [(e["type"], e.get("kind")) for e in events]
            assert ("token", None) in kinds, kinds
            err = [e for e in events if e.get("kind") == "serve_failed"]
            assert err, kinds
            assert err[0]["spent_msat"] == 8000 and err[0]["delivered_tokens"] == 2
            assert not any(e["type"] == "done" for e in events), "must not also report done"
            # host fault -> recorded as a failed attempt
            rep = reputation.load()
            assert rep.get("ab" * 32, {}).get("failures") == 1, rep
    finally:
        srv.shutdown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[serve-failure] {len(fns)} tests PASS")
