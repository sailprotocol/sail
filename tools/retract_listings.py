#!/usr/bin/env python3
"""
Retract (delete) SAIL host listings from the Nostr relays — for clearing dead/zombie test hosts.

A kind-38111 host listing is a *parameterized-replaceable* event; when a host stops re-announcing
it lingers on relays until they age it out. This publishes a NIP-09 kind-5 **deletion** signed by
each host's OWN key, referencing that host's listing coordinate (`38111:<pubkey>:<pubkey>`). Relays
that honor NIP-09 then drop the listing. You can ONLY delete listings signed by keys you hold.

(The client-side freshness filter already HIDES stale hosts regardless of relay cooperation — this
additionally removes them from the relays for keys you still have.)

Usage:
  # keys can be nsec strings OR paths to host key files (each file = one nsec, e.g. HOST_KEY_PATH):
  python tools/retract_listings.py ~/.config/inference-net/host.nsec ~/other-test-host.nsec
  python tools/retract_listings.py nsec1abc...            # an nsec directly
  python tools/retract_listings.py --dry-run <keys...>    # preview: derive pubkeys, publish nothing
  python tools/retract_listings.py --relays wss://relay.damus.io,wss://nos.lol <keys...>

Relays default to $NOSTR_RELAYS (comma-separated) if --relays is omitted.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.listing import LISTING_KIND  # 38111


def _load_key(arg: str):
    """Resolve a positional arg to nostr_sdk Keys: a file path (nsec inside) or an nsec/hex string."""
    from nostr_sdk import Keys
    p = pathlib.Path(os.path.expanduser(arg))
    material = p.read_text().strip() if p.is_file() else arg.strip()
    return Keys.parse(material)


def _relays(cli_relays: str | None) -> list[str]:
    raw = cli_relays if cli_relays is not None else os.getenv("NOSTR_RELAYS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


async def _retract_one(keys, relays: list[str], reason: str) -> dict:
    from nostr_sdk import Client, EventBuilder, Kind, NostrSigner, RelayUrl, Tag
    from datetime import timedelta

    pub = keys.public_key().to_hex()
    coord = f"{LISTING_KIND}:{pub}:{pub}"  # addressable coordinate: kind:pubkey:d-identifier (d = pubkey)
    # NIP-09 deletion: kind 5, `a` tag = the replaceable listing coordinate, `k` tag = its kind.
    builder = EventBuilder(Kind(5), reason).tags(
        [Tag.parse(["a", coord]), Tag.parse(["k", str(LISTING_KIND)])]
    )
    signed = builder.sign_with_keys(keys)

    client = Client(NostrSigner.keys(keys))
    for url in relays:
        await client.add_relay(RelayUrl.parse(url))
    await client.connect()
    try:
        await client.wait_for_connection(timedelta(seconds=10))
    except Exception:  # noqa: BLE001 — send anyway if the wait API/relay isn't cooperative
        pass
    out = await client.send_event(signed)
    await client.shutdown()
    return {"pubkey": pub, "coord": coord, "event_id": out.id.to_hex(),
            "success": [str(u) for u in out.success],
            "failed": {str(u): r for u, r in out.failed.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description="Retract SAIL host listings via NIP-09 kind-5 deletions.")
    ap.add_argument("keys", nargs="+", help="nsec strings or paths to host key files")
    ap.add_argument("--relays", default=None, help="comma-separated relay URLs (default: $NOSTR_RELAYS)")
    ap.add_argument("--reason", default="SAIL host retired", help="deletion reason (event content)")
    ap.add_argument("--dry-run", action="store_true", help="derive pubkeys + show the plan; publish nothing")
    args = ap.parse_args()

    relays = _relays(args.relays)
    if not relays and not args.dry_run:
        print("error: no relays — pass --relays wss://… or set NOSTR_RELAYS", file=sys.stderr)
        return 2

    # Resolve keys first so a bad key/file fails before we publish anything.
    resolved = []
    for arg in args.keys:
        try:
            resolved.append((arg, _load_key(arg)))
        except Exception as e:  # noqa: BLE001
            print(f"error: could not load key from {arg!r}: {e}", file=sys.stderr)
            return 2

    print(f"Retracting {len(resolved)} listing(s) from: {', '.join(relays) or '(none)'}")
    for arg, keys in resolved:
        pub = keys.public_key().to_hex()
        coord = f"{LISTING_KIND}:{pub}:{pub}"
        if args.dry_run:
            print(f"  [dry-run] {pub[:12]}…  would delete coordinate {coord}")
            continue
        try:
            r = asyncio.run(_retract_one(keys, relays, args.reason))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {pub[:12]}…  failed: {e}")
            continue
        ok = ", ".join(r["success"]) or "none"
        bad = "; ".join(f"{u}: {why}" for u, why in r["failed"].items())
        print(f"  ✓ {pub[:12]}…  delete {r['event_id'][:10]}… accepted by: {ok}"
              + (f"  | rejected: {bad}" if bad else ""))
    if args.dry_run:
        print("dry-run: nothing published. Re-run without --dry-run to delete.")
    else:
        print("Done. Relays that honor NIP-09 will drop the listings; the client freshness filter "
              "hides them regardless. Allow a few minutes for propagation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
