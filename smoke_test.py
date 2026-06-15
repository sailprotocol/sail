"""
Phase-0 smoke test — validates the full loop in-process (no server/ports needed):
discover -> 402 challenge -> pay (mock) -> L402 retry -> metered token stream.

Run:  PYTHONPATH=. python3 smoke_test.py
"""
import os
os.environ.setdefault("PAYMENTS", "mock")
os.environ.setdefault("MODEL", "mock")

from fastapi.testclient import TestClient
from host.daemon import app
from shared import registry


def main() -> None:
    with TestClient(app) as c:  # startup publishes the listing
        hosts = registry.discover()
        assert hosts, "no hosts discovered"
        print(f"[test] discovered {len(hosts)} host(s): {hosts[0].models[0].name}")

        r = c.post("/v1/inference", json={"prompt": "hi", "max_tokens": 32})
        assert r.status_code == 402, r.status_code
        ch = r.json()
        print(f"[test] 402 challenge: {ch['amount_msat']} msat")

        preimage = c.post("/mock/pay", json={"payment_hash": ch["payment_hash"]}).json()["preimage"]
        auth = f"L402 {ch['macaroon']}:{preimage}"

        spent, out = 0, []
        with c.stream("POST", "/v1/inference",
                      json={"prompt": "Explain Lightning in one sentence", "max_tokens": 32},
                      headers={"Authorization": auth}) as s:
            assert s.status_code == 200, s.status_code
            for chunk in s.iter_text():
                if "__SPENT_MSAT__:" in chunk:
                    text, _, meta = chunk.partition("__SPENT_MSAT__:")
                    out.append(text); spent = int(meta.strip())
                else:
                    out.append(chunk)

        print("[test] response:", "".join(out).strip())
        print(f"[test] metered spend: {spent} msat (~{spent/1000:.0f} sat)")
        assert spent > 0 and out, "loop failed"
        print("[test] PASS")


if __name__ == "__main__":
    main()
