"""
NIP-13 proof-of-work for listings.

Publishing a host listing costs work: its NIP-01 event id must have at least `target` leading
zero BITS, found by grinding a `["nonce", n, target]` tag. Clients reject listings below their
minimum difficulty, which raises the cost of spamming the offer book.

The real Nostr path mines via nostr-sdk's EventBuilder.pow(); these pure-Python helpers cover
the local/dev path (which has no signed nostr-sdk event) and the shared difficulty check.
"""
from __future__ import annotations

import hashlib
import json


def leading_zero_bits(id_hex: str) -> int:
    """Number of leading zero bits in a hex-encoded 32-byte event id."""
    bits = 0
    for ch in id_hex:
        v = int(ch, 16)
        if v == 0:
            bits += 4
            continue
        for mask in (0b1000, 0b0100, 0b0010, 0b0001):  # stop at the first set bit
            if v & mask:
                return bits
            bits += 1
    return bits


def nip01_id(event: dict) -> str:
    """NIP-01 event id: sha256 over [0,pubkey,created_at,kind,tags,content] (no whitespace)."""
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"],
         event["tags"], event["content"]],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def mine(event: dict, target: int) -> dict:
    """Grind a NIP-13 nonce so the event id has >= `target` leading zero bits.

    Mutates and returns `event`, setting its 'id' and a ["nonce", n, target] tag.
    With target <= 0 it just stamps the id (no work).
    """
    base = [t for t in event.get("tags", []) if not (t and t[0] == "nonce")]
    if target <= 0:
        event["tags"] = base
        event["id"] = nip01_id(event)
        return event
    nonce = 0
    while True:
        event["tags"] = base + [["nonce", str(nonce), str(target)]]
        eid = nip01_id(event)
        if leading_zero_bits(eid) >= target:
            event["id"] = eid
            return event
        nonce += 1
