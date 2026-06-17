"""
L402 (LSAT) handshake helpers.

L402 gates an HTTP resource behind a Lightning payment:
  1. Client requests the resource with no/invalid auth.
  2. Server responds 402 Payment Required with a *macaroon* (an authorization token)
     and a Lightning *invoice*.
  3. Client pays the invoice; paying reveals the *preimage* whose SHA-256 equals the
     invoice's payment_hash.
  4. Client retries with header:  Authorization: L402 <macaroon>:<preimage>
  5. Server verifies sha256(preimage) == payment_hash bound to that macaroon, then serves.

This module implements the real handshake shape with real preimage/hash crypto. The
*source* of the preimage is abstracted by the payments backend: mock reveals it locally;
LND reveals it only after a real settled payment (Phase 1).
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- metered settlement (chunked L402) -------------------------------------
# A paid chunk's response is a text/plain stream of tokens followed by ONE trailer line:
#   __L402_NEXT__:<json challenge>   another chunk remains; pay it to continue
#   __L402_DONE__:<json {spent_msat}>  generation finished; stop
# Reuses the in-band trailer convention of __SPENT_MSAT__ so the stream stays plain text.
NEXT_MARKER = "__L402_NEXT__:"
DONE_MARKER = "__L402_DONE__:"


def next_trailer(challenge: dict) -> str:
    """Trailer telling the client to pay `challenge` for the next chunk."""
    return NEXT_MARKER + json.dumps(challenge)


def done_trailer(spent_msat: int) -> str:
    """Trailer telling the client generation is complete and what it was billed."""
    return DONE_MARKER + json.dumps({"spent_msat": spent_msat})


@dataclass
class L402Challenge:
    macaroon: str        # opaque authorization token bound to a payment_hash
    invoice: str         # BOLT11 (mock string in Phase 0)
    payment_hash: str    # hex; sha256(preimage)
    amount_msat: int

    def www_authenticate(self) -> str:
        return f'L402 macaroon="{self.macaroon}", invoice="{self.invoice}"'


def new_macaroon() -> str:
    # Phase 1: a real macaroon with caveats (model, max tokens, expiry).
    return "mac_" + secrets.token_hex(16)


def parse_authorization(header: str | None) -> tuple[str, str] | None:
    """Parse 'L402 <macaroon>:<preimage>' -> (macaroon, preimage)."""
    if not header or not header.startswith("L402 "):
        return None
    token = header[len("L402 "):].strip()
    if ":" not in token:
        return None
    macaroon, preimage = token.split(":", 1)
    return macaroon, preimage


def verify(preimage_hex: str, payment_hash_hex: str) -> bool:
    """Real check: the preimage must hash to the bound payment_hash."""
    try:
        return sha256_hex(bytes.fromhex(preimage_hex)) == payment_hash_hex
    except ValueError:
        return False
