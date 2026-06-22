"""
Lightning backends.

The host issues invoices and verifies payment via one of these. Phase 0 uses
MockLightning (self-contained, no node). Phase 1 swaps in LndLightning.

Select with env: PAYMENTS=mock (default) | lnd | phoenixd
"""
from __future__ import annotations

import base64
import math
import os
import pathlib
import secrets

import httpx

from shared.l402 import sha256_hex


def _whole_sats(amount_msat: int) -> int:
    """msat -> whole sats for backends that mint BOLT11 in sats (phoenixd). Positive, with a
    1-sat floor so sub-sat pricing can't request an un-mintable invoice."""
    if amount_msat is None or amount_msat <= 0:
        raise ValueError(f"invoice amount must be positive msat, got {amount_msat!r}")
    return max(1, math.ceil(amount_msat / 1000))


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


class PhoenixdLightning(LightningBackend):
    """phoenixd — self-custodial node with auto-liquidity, over its local HTTP API.

    The operator runs phoenixd (Apache-2.0, by ACINQ) which serves http://127.0.0.1:9740 with
    HTTP Basic auth (empty username + the auto-generated http-password). It mints normal BOLT11
    invoices, so the L402 flow / client / relays are unchanged. Connection details from env only:
      PHOENIXD_API_URL       default http://127.0.0.1:9740
      PHOENIXD_API_PASSWORD  the http-password from ~/.phoenix/phoenix.conf (secret)
    """

    def __init__(self) -> None:
        base = os.getenv("PHOENIXD_API_URL", "http://127.0.0.1:9740").rstrip("/")
        password = os.environ["PHOENIXD_API_PASSWORD"]
        # Basic auth, empty username + the http-password (phoenixd's scheme). localhost -> fast.
        self._client = httpx.Client(base_url=base, auth=("", password), timeout=10.0)

    def create_invoice(self, amount_msat: int, expiry_seconds: int | None = None) -> tuple[str, str]:
        data = {"amountSat": str(_whole_sats(amount_msat)), "description": "SAIL inference"}
        if expiry_seconds is not None:
            data["expirySeconds"] = str(expiry_seconds)
        try:
            r = self._client.post("/createinvoice", data=data)
        except httpx.HTTPError as e:
            raise RuntimeError(f"phoenixd unreachable at {self._client.base_url}: {e}") from e
        r.raise_for_status()
        j = r.json()
        return j["serialized"], j["paymentHash"]  # serialized = BOLT11

    def is_settled(self, payment_hash_hex: str) -> bool:
        try:
            r = self._client.get(f"/payments/incoming/{payment_hash_hex}")
        except httpx.HTTPError:
            return False  # phoenixd not ready / unreachable -> treat as unpaid
        if r.status_code != 200:
            return False  # unknown hash or error -> unpaid
        return bool(r.json().get("isPaid"))


def get_backend() -> LightningBackend:
    kind = os.getenv("PAYMENTS", "mock").lower()
    if kind == "mock":
        return MockLightning()
    if kind == "lnd":
        return LndLightning()
    if kind == "phoenixd":
        return PhoenixdLightning()
    raise ValueError(f"unknown PAYMENTS backend: {kind}")
