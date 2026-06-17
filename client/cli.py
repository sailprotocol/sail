"""
Client CLI.

Discovers hosts, picks one, completes the L402 handshake (pays the invoice), and streams
the response while tallying what it spent.

    python -m client.cli "Explain Lightning in one sentence"

Phase 0 "pays" via the host's /mock/pay shim. Phase 1 pays via the client's own LN node
(the act of paying reveals the preimage), and discovery reads Nostr instead of ./registry.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import httpx

from shared import registry
from shared.config import load_env


def pick_host():
    hosts = registry.discover()
    if not hosts:
        sys.exit("No hosts found. Start a host daemon first (see README).")
    h = hosts[0]  # Phase 2: rank by price * reputation
    print(f"[client] using host {h.pubkey} -> {h.models[0].name} @ {h.endpoint}")
    return h


def pay_invoice(endpoint: str, ch: dict) -> str:
    """Obtain the preimage for the challenge by paying.

    PAYMENTS=mock: ask the host's /mock/pay shim to settle and reveal it (Phase 0).
    PAYMENTS=lnd:  pay the BOLT11 invoice via the client's own LND node; the act of
                   paying reveals the preimage.
    """
    if os.getenv("PAYMENTS", "mock").lower() == "lnd":
        return lnd_pay(ch["invoice"])
    r = httpx.post(f"{endpoint}/mock/pay", json={"payment_hash": ch["payment_hash"]})
    r.raise_for_status()
    return r.json()["preimage"]


def lnd_pay(invoice: str) -> str:
    """Pay a BOLT11 invoice via this client's LND node (REST SendPaymentV2) and return
    the payment preimage (hex). Reads the node's connection details from env only."""
    host = os.environ["LND_REST_HOST"].rstrip("/")
    cert = os.environ["LND_TLS_CERT_PATH"]
    macaroon = pathlib.Path(os.environ["LND_MACAROON_PATH"]).read_bytes().hex()
    body = {
        "payment_request": invoice,
        "timeout_seconds": 60,
        "fee_limit_msat": "10000",   # generous; regtest direct-channel fees are ~0
        "no_inflight_updates": True,  # stream only the final result
    }
    with httpx.stream(
        "POST", f"{host}/v2/router/send", json=body,
        headers={"Grpc-Metadata-macaroon": macaroon},
        verify=cert, timeout=70.0,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if "error" in msg:
                sys.exit(f"LND payment error: {msg['error']}")
            result = msg.get("result", {})
            status = result.get("status")
            if status == "SUCCEEDED":
                return result["payment_preimage"]
            if status == "FAILED":
                sys.exit(f"payment failed: {result.get('failure_reason', 'unknown')}")
    sys.exit("payment stream ended without a SUCCEEDED result")


def main() -> None:
    load_env()
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
    preimage = pay_invoice(ep, ch)

    # 3) retry with L402 auth, stream the response.
    # Generous read timeout for THIS request only: a cold-loading reasoning model
    # (e.g. qwen3:14b) can take well over httpx's 5s default to emit its first token.
    # The quick 402/payment calls above keep the short default; a hung host is still bounded.
    auth = f"L402 {ch['macaroon']}:{preimage}"
    stream_timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
    print("[client] response:\n")
    spent_msat = 0
    with httpx.stream("POST", f"{ep}/v1/inference",
                      json={"prompt": prompt, "max_tokens": 64},
                      headers={"Authorization": auth},
                      timeout=stream_timeout) as s:
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
