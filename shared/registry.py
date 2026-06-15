"""
Discovery transport.

Phase 0: hosts write their listing to ./registry/<pubkey>.json and clients read the dir.
Phase 1: replace both functions with Nostr relay publish/subscribe over Tor. The
HostListing.to_nostr_event() / from_nostr_event() shape is already the real one, so only
this transport changes.
"""
from __future__ import annotations

import json
import os
import pathlib

from shared.listing import HostListing

REGISTRY_DIR = pathlib.Path(os.getenv("REGISTRY_DIR", "./registry"))


def publish(listing: HostListing) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    event = listing.to_nostr_event()
    (REGISTRY_DIR / f"{listing.pubkey}.json").write_text(json.dumps(event))


def discover() -> list[HostListing]:
    if not REGISTRY_DIR.exists():
        return []
    out = []
    for f in REGISTRY_DIR.glob("*.json"):
        try:
            out.append(HostListing.from_nostr_event(json.loads(f.read_text())))
        except Exception:
            continue
    return out
