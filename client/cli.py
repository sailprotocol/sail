"""
Client CLI.

Discovers hosts, ranks them by local reputation, completes the metered L402 handshake against
the best one, and streams the response while tallying sats. All the real logic lives in
`client/core.py` (shared with the web app, `client/webapp.py`); this is a thin terminal frontend.

    python -m client.cli "Explain Lightning in one sentence"
"""
from __future__ import annotations

import sys

from shared.config import load_env
from shared.alias import alias_label
from client import core


def main() -> None:
    load_env()
    prompt = " ".join(sys.argv[1:]) or "Say hello from the network."

    hosts = core.discover_hosts()
    if not hosts:
        sys.exit("No hosts found (none discovered, all below the PoW minimum, "
                 "or dropped by local reputation).")
    host = hosts[0]  # best-ranked
    bond = f" | bond {host.bond_txid} (advisory)" if getattr(host, "bond_txid", None) else ""
    # Alias derived from the verified pubkey (shared.alias); the pubkey stays the source of truth.
    print(f"[client] using host {alias_label(host.pubkey)}  [{host.pubkey}]")
    print(f"[client]   -> {host.models[0].name} @ {host.endpoint}{bond}")
    if core.proxy_for(host.endpoint):
        print("[client] .onion host -> routing over Tor")

    print("[client] response:\n")
    for ev in core.run_inference(host, prompt, max_tokens=64):
        if ev["type"] == "token":
            sys.stdout.write(ev["text"]); sys.stdout.flush()
        elif ev["type"] == "done":
            print(f"\n\n[client] done. spent {ev['spent_msat']} msat "
                  f"(~{ev['spent_msat'] / 1000:.0f} sat) in {ev['latency_ms'] / 1000:.1f}s.")
        elif ev["type"] == "error":
            if ev.get("kind") == "unreachable":
                sys.exit(f"\n[client] host unreachable — try another host or check the network "
                         f"({ev['message']})")
            sys.exit(f"\n[client] error: {ev['message']}")


if __name__ == "__main__":
    main()
