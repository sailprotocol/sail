"""
Lightning backends.

The host issues invoices and verifies payment via one of these. Phase 0 uses
MockLightning (self-contained, no node). Phase 1 swaps in LndLightning.

Select with env: PAYMENTS=mock (default) | lnd
"""
from __future__ import annotations

import base64
import os
import pathlib
import secrets

import httpx

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
    """Real LND over REST. Issues BOLT11 invoices via this host's own node.

    Connection details come from env only (never hardcoded/committed):
      LND_REST_HOST       e.g. https://127.0.0.1:8084
      LND_TLS_CERT_PATH   path to that node's tls.cert (used as httpx TLS verify)
      LND_MACAROON_PATH   path to that node's admin.macaroon (sent hex-encoded)

    The host never learns the preimage — the payer reveals it by paying — so
    reveal_preimage stays unimplemented (and /mock/pay is disabled when PAYMENTS=lnd).
    """

    def __init__(self) -> None:
        host = os.environ["LND_REST_HOST"].rstrip("/")
        cert = os.environ["LND_TLS_CERT_PATH"]
        macaroon = pathlib.Path(os.environ["LND_MACAROON_PATH"]).read_bytes().hex()
        self._client = httpx.Client(
            base_url=host,
            verify=cert,
            headers={"Grpc-Metadata-macaroon": macaroon},
            timeout=10.0,
        )

    def create_invoice(self, amount_msat: int) -> tuple[str, str]:
        r = self._client.post("/v1/invoices", json={"value_msat": str(amount_msat)})
        r.raise_for_status()
        data = r.json()
        # r_hash is base64-encoded bytes in LND's REST gateway; we want hex.
        payment_hash = base64.b64decode(data["r_hash"]).hex()
        return data["payment_request"], payment_hash


def get_backend() -> LightningBackend:
    kind = os.getenv("PAYMENTS", "mock").lower()
    if kind == "mock":
        return MockLightning()
    if kind == "lnd":
        return LndLightning()
    raise ValueError(f"unknown PAYMENTS backend: {kind}")
