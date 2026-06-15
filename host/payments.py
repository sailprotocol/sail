"""
Lightning backends.

The host issues invoices and verifies payment via one of these. Phase 0 uses
MockLightning (self-contained, no node). Phase 1 swaps in LndLightning.

Select with env: PAYMENTS=mock (default) | lnd
"""
from __future__ import annotations

import os
import secrets
from shared.l402 import sha256_hex


class LightningBackend:
    def create_invoice(self, amount_msat: int) -> tuple[str, str]:
        """Return (bolt11, payment_hash_hex)."""
        raise NotImplementedError

    def reveal_preimage(self, payment_hash_hex: str) -> str | None:
        """
        Phase-0 ONLY shim: simulate the LN network settling a payment and revealing the
        preimage to the payer. In production the *client's* node learns the preimage by
        paying; the host never hands it out. This exists purely so the local loop runs.
        """
        raise NotImplementedError


class MockLightning(LightningBackend):
    """No real node. Generates a real preimage/hash pair and 'settles' on demand."""

    def __init__(self) -> None:
        self._hash_to_preimage: dict[str, str] = {}

    def create_invoice(self, amount_msat: int) -> tuple[str, str]:
        preimage = secrets.token_bytes(32)
        payment_hash = sha256_hex(preimage)
        self._hash_to_preimage[payment_hash] = preimage.hex()
        bolt11 = f"lnbcMOCK{amount_msat}m{payment_hash[:12]}"
        return bolt11, payment_hash

    def reveal_preimage(self, payment_hash_hex: str) -> str | None:
        return self._hash_to_preimage.get(payment_hash_hex)


class LndLightning(LightningBackend):
    """Phase 1: talk to LND via gRPC/REST. Stub on purpose."""

    def __init__(self) -> None:
        # TODO: load LND_GRPC_HOST, tls cert, macaroon path from env.
        raise NotImplementedError(
            "Phase 1: wire LND here (addinvoice / lookupinvoice). "
            "Use a dedicated node, NOT the AUPA BTCPay node."
        )


def get_backend() -> LightningBackend:
    kind = os.getenv("PAYMENTS", "mock").lower()
    if kind == "mock":
        return MockLightning()
    if kind == "lnd":
        return LndLightning()
    raise ValueError(f"unknown PAYMENTS backend: {kind}")
