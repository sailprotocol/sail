"""
Phase-0 smoke test — validates the full loop in-process (no server/ports needed):
discover -> 402 challenge -> pay (mock) -> L402 retry -> metered token stream.

Run:  PYTHONPATH=. python3 smoke_test.py
"""
import json
import os
import types
os.environ.setdefault("PAYMENTS", "mock")
os.environ.setdefault("MODEL", "mock")
os.environ.setdefault("POW_TARGET", "8")          # trivial difficulty: mines instantly
os.environ.setdefault("POW_MIN_DIFFICULTY", "8")  # client rejects listings below this
os.environ.setdefault("MODEL_ALLOWLIST", "mock-echo:1b")  # enforce allowlist; mock is allowed

from fastapi.testclient import TestClient
from host.daemon import app, PRICE_MSAT_PER_TOKEN
from shared import registry
from shared.l402 import NEXT_MARKER, DONE_MARKER
from shared import pow as powmod
from host import moderation


def check_moderation() -> None:
    # Allow path: the served mock model is on the allowlist (set above).
    assert moderation.is_model_allowed("mock-echo:1b"), "allowlisted model should serve"
    # Deny path: a model not on the allowlist is refused.
    assert not moderation.is_model_allowed("evil-model:70b"), "non-allowlisted model must be refused"
    # CSAM gate is fail-closed: no matcher configured -> image output blocked.
    try:
        moderation.check_image_output(b"\x89PNG...")
        raise AssertionError("check_image_output must raise without a configured matcher")
    except moderation.ModerationError:
        pass
    # Image-modality model refused to serve without a matcher.
    img_model = types.SimpleNamespace(name="mock-echo:1b", modality="image")
    try:
        moderation.assert_can_serve(img_model)
        raise AssertionError("image-modality model must be refused without a CSAM matcher")
    except moderation.ModerationError:
        pass
    print("[test] moderation: allowlist enforced, image gate fail-closed")


def main() -> None:
    check_moderation()
    with TestClient(app) as c:  # startup publishes the (PoW-mined) listing
        hosts = registry.discover()
        assert hosts, "no hosts discovered (PoW gate too strict, or publish failed)"
        print(f"[test] discovered {len(hosts)} host(s): {hosts[0].models[0].name}")

        # PoW: the published listing must actually meet the minimum difficulty.
        import pathlib
        reg_dir = pathlib.Path(os.getenv("REGISTRY_DIR", "./registry"))
        ev = json.loads(next(reg_dir.glob("*.json")).read_text())
        bits = powmod.leading_zero_bits(powmod.nip01_id(ev))
        assert bits >= 8, f"listing difficulty {bits} < 8"
        print(f"[test] listing PoW difficulty: {bits} bits (>= 8)")

        # Negative check: a spam listing below the minimum is rejected by discover().
        # Deterministically grind an id with < 8 leading-zero bits (found almost immediately).
        spam = dict(ev)
        spam["pubkey"] = "spam_host"
        base_tags = [t for t in spam["tags"] if t[0] != "nonce"]
        n = 0
        while True:
            spam["tags"] = base_tags + [["x", str(n)]]
            spam["id"] = powmod.nip01_id(spam)
            if powmod.leading_zero_bits(spam["id"]) < 8:
                break
            n += 1
        (reg_dir / "spam_host.json").write_text(json.dumps(spam))
        assert all(h.pubkey != "spam_host" for h in registry.discover()), "under-PoW spam not rejected"
        print("[test] under-PoW spam listing rejected")
        (reg_dir / "spam_host.json").unlink()

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
