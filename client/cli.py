"""
Client CLI.

Discovers hosts, picks one, completes the L402 handshake (pays the invoice), and streams
the response while tallying what it spent.

    python -m client.cli "Explain Lightning in one sentence"

Phase 0 "pays" via the host's /mock/pay shim. Phase 1 pays via the client's own LN node
(the act of paying reveals the preimage), and discovery reads Nostr instead of ./registry.
"""
from __future__ import annotations

import sys
import httpx

from shared import registry


def pick_host():
    hosts = registry.discover()
    if not hosts:
        sys.exit("No hosts found. Start a host daemon first (see README).")
    h = hosts[0]  # Phase 2: rank by price * reputation
    print(f"[client] using host {h.pubkey} -> {h.models[0].name} @ {h.endpoint}")
    return h


def pay_invoice(endpoint: str, payment_hash: str) -> str:
    """Phase 0: ask the mock shim to settle and reveal the preimage.
    Phase 1: replace with a real LN payment via the client's node."""
    r = httpx.post(f"{endpoint}/mock/pay", json={"payment_hash": payment_hash})
    r.raise_for_status()
    return r.json()["preimage"]


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Say hello from the network."
    host = pick_host()
    ep = host.endpoint

    # 1) request inference, expect a 402 challenge
    r = httpx.post(f"{ep}/v1/inference", json={"prompt": prompt, "max_tokens": 64})
    if r.status_code != 402:
        sys.exit(f"expected 402, got {r.status_code}: {r.text}")
    ch = r.json()
    print(f"[client] 402: invoice for {ch['amount_msat']} msat "
          f"({ch['amount_msat'] / 1000:.0f} sat). paying...")

    # 2) pay -> obtain preimage
    preimage = pay_invoice(ep, ch["payment_hash"])

    # 3) retry with L402 auth, stream the response
    auth = f"L402 {ch['macaroon']}:{preimage}"
    print("[client] response:\n")
    spent_msat = 0
    with httpx.stream("POST", f"{ep}/v1/inference",
                      json={"prompt": prompt, "max_tokens": 64},
                      headers={"Authorization": auth}) as s:
        for chunk in s.iter_text():
            if "__SPENT_MSAT__:" in chunk:
                text, _, meta = chunk.partition("__SPENT_MSAT__:")
                sys.stdout.write(text)
                spent_msat = int(meta.strip())
            else:
                sys.stdout.write(chunk)
            sys.stdout.flush()

    print(f"\n\n[client] done. spent {spent_msat} msat "
          f"(~{spent_msat / 1000:.0f} sat).")


if __name__ == "__main__":
    main()
