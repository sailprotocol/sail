"""
Client CLI.

Discovers hosts, ranks them by local reputation, completes the metered L402 handshake against
the best one, and streams the response while tallying sats. All the real logic lives in
`client/core.py` (shared with the web app, `client/webapp.py`); this is a thin terminal frontend.

    python -m client.cli "Explain Lightning in one sentence"
    python -m client.cli --list                # show discoverable hosts + what's filtered
    python -m client.cli --reputation          # inspect the local reputation store
    python -m client.cli --reset-reputation    # clear it (un-bury hosts dropped by past errors)
"""
from __future__ import annotations

import sys

from shared.config import load_env
from shared.alias import alias_label
from client import core, reputation


def _cmd_list() -> None:
    """Discovery only — never touches a pay path. Shows kept hosts and what was filtered out."""
    d = core.discover_hosts_detailed()
    for h in d["hosts"]:
        print(f"  {alias_label(h.pubkey)}  [{h.pubkey[:16]}…]  {h.models[0].name} @ {h.endpoint}")
    if not d["hosts"]:
        print("  (no hosts available)")
    hidden = []
    if d["rep_hidden"]:
        hidden.append(f"{d['rep_hidden']} by local reputation")
    if d["pow_rejected"]:
        hidden.append(f"{d['pow_rejected']} by PoW")
    if d["allowlist_hidden"]:
        hidden.append(f"{d['allowlist_hidden']} by model allowlist")
    if d["sig_rejected"]:
        hidden.append(f"{d['sig_rejected']} by bad signature")
    if d["parse_rejected"]:
        hidden.append(f"{d['parse_rejected']} by parse error")
    print(f"[client] {len(d['hosts'])} shown" + (f" · hidden: {', '.join(hidden)}" if hidden else ""))
    # Per-listing PoW detail: measured < required, with which host — so a hidden host is
    # diagnosable (e.g. "published with 0 bits" = host isn't grinding PoW) instead of a guess.
    for p in d["pow_hidden"]:
        print(f"    - {alias_label(p['pubkey'])}  PoW {p['bits']}<{p['required']}"
              + ("  (host published with no/low PoW — raise its POW_TARGET)" if p["bits"] < p["required"] // 2 else ""))
    if d["rep_hidden"]:
        print("[client] un-bury reputation-hidden hosts with: python -m client.cli --reset-reputation")


def _cmd_reputation() -> None:
    rep = reputation.load()
    if not rep:
        print("[client] local reputation store is empty.")
        return
    for pk, s in rep.items():
        rate = (s["successes"] / s["attempts"]) if s.get("attempts") else 0.0
        print(f"  {alias_label(pk)}  [{pk[:16]}…]  {s.get('successes',0)}/{s.get('attempts',0)} ok "
              f"({rate:.0%}) · {s.get('consecutive_failures',0)} consec-fail")


def _cmd_reset_reputation() -> None:
    print("[client] local reputation cleared." if reputation.reset()
          else "[client] no local reputation store to clear.")


def main() -> None:
    load_env()
    argv = sys.argv[1:]
    if argv and argv[0] == "--list":
        return _cmd_list()
    if argv and argv[0] == "--reputation":
        return _cmd_reputation()
    if argv and argv[0] == "--reset-reputation":
        return _cmd_reset_reputation()
    prompt = " ".join(argv) or "Say hello from the network."

    hosts = core.discover_hosts()
    if not hosts:
        sys.exit("No hosts found (none discovered, all below the PoW minimum, or dropped by local "
                 "reputation). Try: python -m client.cli --list")
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
            if ev.get("kind") == "serve_failed":
                sats = (ev.get("spent_msat") or 0) / 1000
                sys.exit(f"\n\n[client] host failed to serve — {ev['message']}. "
                         f"{sats:.0f} sat spent for {ev.get('delivered_tokens', 0)} token(s) "
                         f"delivered (partial output above; no further chunks charged).")
            if ev.get("kind") == "config":
                sys.exit(f"\n[client] {ev['message']}.\n[client] This is a client-side mode "
                         f"mismatch — the host wasn't penalized. Configure a wallet (PAYMENTS=nwc "
                         f"or lnd) or use --list to see hosts.")
            if ev.get("kind") == "unreachable":
                sys.exit(f"\n[client] host unreachable — try another host or check the network "
                         f"({ev['message']})")
            sys.exit(f"\n[client] error: {ev['message']}")


if __name__ == "__main__":
    main()
