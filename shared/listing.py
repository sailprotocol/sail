"""
Host listing schema.

In production a listing is a *signed Nostr event* that hosts publish to relays and
clients discover by subscribing. For the Phase-0 proof-of-loop we serialize the same
structure to a local ./registry/ directory so the loop runs with no network.

The shape here is deliberately the real one, so swapping the transport (local dir ->
Nostr relays) in Phase 1 does not change the data model.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

# Nostr event kind we reserve for inference host listings (parameterized replaceable
# range so a host's latest listing supersedes its previous one).
LISTING_KIND = 38_111


@dataclass
class ModelOffer:
    name: str                 # e.g. "llama3.1:70b"
    price_msat_per_token: int  # price per output token, in millisats
    context_window: int
    modality: str = "text"     # "text" | "image" | "code"


@dataclass
class HostListing:
    pubkey: str                # host Nostr pubkey (hex). Identity + payment routing key.
    endpoint: str              # how to reach the host. Prod: .onion. Dev: http://127.0.0.1:PORT
    models: list[ModelOffer]
    reputation: float = 0.0    # filled by the reputation layer (Phase 2); 0.0 at bootstrap
    bond_txid: Optional[str] = None  # proof-of-bond (Phase 2)
    pow_nonce: int = 0         # Proof-of-Work nonce for anti-spam (Phase 2)
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_nostr_event(self) -> dict:
        """Produce the (unsigned) Nostr event. Phase 1 attaches a real schnorr sig."""
        return {
            "kind": LISTING_KIND,
            "pubkey": self.pubkey,
            "created_at": self.updated_at,
            "tags": [["d", self.pubkey], ["n", "inference-net-v0"]],
            "content": json.dumps(
                {
                    "endpoint": self.endpoint,
                    "models": [asdict(m) for m in self.models],
                    "reputation": self.reputation,
                    "bond_txid": self.bond_txid,
                }
            ),
            # "sig": <schnorr sig over the serialized event>  # TODO Phase 1
        }

    @staticmethod
    def from_nostr_event(event: dict) -> "HostListing":
        c = json.loads(event["content"])
        return HostListing(
            pubkey=event["pubkey"],
            endpoint=c["endpoint"],
            models=[ModelOffer(**m) for m in c["models"]],
            reputation=c.get("reputation", 0.0),
            bond_txid=c.get("bond_txid"),
            updated_at=event.get("created_at", 0),
        )
