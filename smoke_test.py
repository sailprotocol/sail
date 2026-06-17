"""
Phase-0 smoke test — validates the full loop in-process (no server/ports needed):
discover -> 402 challenge -> pay (mock) -> L402 retry -> metered token stream.

Run:  PYTHONPATH=. python3 smoke_test.py
"""
import json
import os
os.environ.setdefault("PAYMENTS", "mock")
os.environ.setdefault("MODEL", "mock")

from fastapi.testclient import TestClient
from host.daemon import app, PRICE_MSAT_PER_TOKEN
from shared import registry
from shared.l402 import NEXT_MARKER, DONE_MARKER


def main() -> None:
    with TestClient(app) as c:  # startup publishes the listing
        hosts = registry.discover()
        assert hosts, "no hosts discovered"
        print(f"[test] discovered {len(hosts)} host(s): {hosts[0].models[0].name}")

        max_tokens = 64  # deliberately larger than the mock reply, to expose the prepay gap
        r = c.post("/v1/inference", json={"prompt": "Explain Lightning in one sentence",
                                          "max_tokens": max_tokens})
        assert r.status_code == 402, r.status_code
        ch = r.json()
        print(f"[test] 402: first chunk {ch['amount_msat']} msat")

        # Metered loop: pay each chunk, stream it, follow the trailer until done.
        spent, chunks, out = 0, 0, []
        while True:
            preimage = c.post("/mock/pay",
                              json={"payment_hash": ch["payment_hash"]}).json()["preimage"]
            spent += ch["amount_msat"]
            auth = f"L402 {ch['macaroon']}:{preimage}"
            with c.stream("POST", "/v1/inference",
                          json={"session_id": ch.get("session_id")},
                          headers={"Authorization": auth}) as s:
                assert s.status_code == 200, s.status_code
                body = "".join(s.iter_text())
            chunks += 1
            if DONE_MARKER in body:
                out.append(body.partition(DONE_MARKER)[0]); break
            assert NEXT_MARKER in body, "chunk had no trailer"
            text, _, meta = body.partition(NEXT_MARKER)
            out.append(text); ch = json.loads(meta)

        response = "".join(out).strip()
        print("[test] response:", response)
        print(f"[test] paid {chunks} chunk(s); metered spend: {spent} msat (~{spent/1000:.0f} sat)")

        assert spent > 0 and response, "loop failed"
        # The fix: pay for what was delivered, NOT the full max_tokens prepay.
        assert spent < max_tokens * PRICE_MSAT_PER_TOKEN, \
            f"expected metered spend < full prepay {max_tokens * PRICE_MSAT_PER_TOKEN}, got {spent}"
        print("[test] PASS")


if __name__ == "__main__":
    main()
