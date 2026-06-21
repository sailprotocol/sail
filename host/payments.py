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
    def create_invoice(self, amount_msat: int, expiry_seconds: int | None = None) -> tuple[str, str]:
        """Return (bolt11, payment_hash_hex). `expiry_seconds` sets the invoice's OWN expiry so a
        wallet cannot pay it after the host has stopped honoring it (BOLT11 fallback). None = node default."""
        raise NotImplementedError

    def reveal_preimage(self, payment_hash_hex: str) -> str | None:
        """
        Phase-0 ONLY shim: simulate the LN network settling a payment and revealing the
        preimage to the payer. In production the *client's* node learns the preimage by
        paying; the host never hands it out. This exists purely so the local loop runs.
        """
        raise NotImplementedError

    def is_settled(self, payment_hash_hex: str) -> bool:
        """Has the invoice been paid? Used by the manual BOLT11 fallback, where a foreign wallet
        pays and the host confirms settlement against its own node (not via a revealed preimage)."""
        raise NotImplementedError


class MockLightning(LightningBackend):
    """No real node. Generates a real preimage/hash pair and 'settles' on demand."""

    def __init__(self) -> None:
        self._hash_to_preimage: dict[str, str] = {}
        self._settled: set[str] = set()
        self._invoice_expiry: dict[str, int | None] = {}  # records the expiry create_invoice got

    def create_invoice(self, amount_msat: int, expiry_seconds: int | None = None) -> tuple[str, str]:
        preimage = secrets.token_bytes(32)
        payment_hash = sha256_hex(preimage)
        self._hash_to_preimage[payment_hash] = preimage.hex()
        self._invoice_expiry[payment_hash] = expiry_seconds
        bolt11 = f"lnbcMOCK{amount_msat}m{payment_hash[:12]}"
        return bolt11, payment_hash

    def reveal_preimage(self, payment_hash_hex: str) -> str | None:
        return self._hash_to_preimage.get(payment_hash_hex)

    def mark_settled(self, payment_hash_hex: str) -> None:
        """Mock-only: simulate a foreign wallet paying the invoice (driven by /mock/settle)."""
        self._settled.add(payment_hash_hex)

    def is_settled(self, payment_hash_hex: str) -> bool:
        return payment_hash_hex in self._settled


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

    def create_invoice(self, amount_msat: int, expiry_seconds: int | None = None) -> tuple[str, str]:
        body = {"value_msat": str(amount_msat)}
        if expiry_seconds is not None:
            body["expiry"] = str(expiry_seconds)  # LND addinvoice expiry (s); else node default
        r = self._client.post("/v1/invoices", json=body)
        r.raise_for_status()
        data = r.json()
        # r_hash is base64-encoded bytes in LND's REST gateway; we want hex.
        payment_hash = base64.b64decode(data["r_hash"]).hex()
        return data["payment_request"], payment_hash

    def is_settled(self, payment_hash_hex: str) -> bool:
        # lookupinvoice by hex payment hash; SETTLED means a (foreign) wallet paid it.
        r = self._client.get(f"/v1/invoice/{payment_hash_hex}")
        if r.status_code != 200:
            return False
        return r.json().get("state") == "SETTLED"


def get_backend() -> LightningBackend:
    kind = os.getenv("PAYMENTS", "mock").lower()
    if kind == "mock":
        return MockLightning()
    if kind == "lnd":
        return LndLightning()
    raise ValueError(f"unknown PAYMENTS backend: {kind}")
