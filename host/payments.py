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

    def ping(self) -> tuple[bool, str]:
        """Is the payment backend's API reachable? Used by go-live / status to refuse to declare a
        host 'live' (payable) when its node/wallet can't actually issue invoices. Default: ok."""
        return True, "ok"

    def receive_status(self) -> dict:
        """Can the backend actually RECEIVE right now? Most backends always can — only phoenixd has
        the channel cliff (no inbound channel until a bootstrap payment). Default: receivable.
        Returns {receivable: bool|None, detail: str, ...extras}."""
        return {"receivable": True, "detail": "ok"}


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

    def ping(self) -> tuple[bool, str]:
        try:
            r = self._client.get("/v1/getinfo")
        except httpx.HTTPError as e:
            return False, f"LND unreachable: {str(e)[:80]}"
        return (r.status_code == 200, "LND ok" if r.status_code == 200 else f"LND HTTP {r.status_code}")


class PhoenixdLightning(LightningBackend):
    """phoenixd — self-custodial node with auto-liquidity, over its local HTTP API.

    The operator runs phoenixd (Apache-2.0, by ACINQ) which serves http://127.0.0.1:9740 with
    HTTP Basic auth (empty username + the auto-generated http-password). It mints normal BOLT11
    invoices, so the L402 flow / client / relays are unchanged. Connection details from env only:
      PHOENIXD_API_URL       default http://127.0.0.1:9740
      PHOENIXD_API_PASSWORD  the http-password from ~/.phoenix/phoenix.conf (secret)
    """

    def __init__(self) -> None:
        from host import phoenixd_setup  # local import (platform glue) — avoids an import cycle
        base = os.getenv("PHOENIXD_API_URL", "http://127.0.0.1:9740").rstrip("/")
        # Source of truth is phoenix.conf (resolve_api_password), so the password can't drift from
        # the active wallet on import/restore/re-provision. PHOENIXD_API_PASSWORD is only a fallback.
        password = phoenixd_setup.resolve_api_password()
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

    def ping(self) -> tuple[bool, str]:
        try:
            r = self._client.get("/getinfo")
        except httpx.HTTPError as e:
            return False, f"phoenixd unreachable: {str(e)[:80]}"
        return (r.status_code == 200, "phoenixd ok" if r.status_code == 200
                else f"phoenixd HTTP {r.status_code}")

    def receive_status(self) -> dict:
        """The channel cliff: phoenixd can't RECEIVE until an inbound channel exists. A brand-new
        node holds the first inbound payment as feeCreditSat with NO channel (listchannels empty),
        so it can't be paid until ~25-35k sat opens one. Detect that and report it so go-live /
        the dashboard don't claim 'live to earn' when the host silently can't earn."""
        try:
            r = self._client.get("/listchannels")
            r.raise_for_status()
            channels = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {"receivable": None, "detail": f"phoenixd unreachable: {str(e)[:80]}"}
        channels = channels if isinstance(channels, list) else []
        # phoenixd reports the channel state in "type" as a fully-qualified lightning-kmp class
        # name, e.g. "fr.acinq.lightning.channel.states.Normal" (NOT a bare "Normal", and there is
        # no top-level "state" field). The short state is the last dotted component; fall back to a
        # legacy "state" field if present. A "Normal" channel is open and usable -> receivable.
        def _state(c: dict) -> str:
            raw = c.get("type") or c.get("state") or ""
            return str(raw).rsplit(".", 1)[-1]
        states = [_state(c) for c in channels if isinstance(c, dict)]
        receivable = "Normal" in states  # an open, usable channel

        balance_sat = fee_credit_sat = None
        try:
            b = self._client.get("/getbalance")
            if b.status_code == 200:
                j = b.json()
                balance_sat, fee_credit_sat = j.get("balanceSat"), j.get("feeCreditSat")
        except httpx.HTTPError:
            pass

        if receivable:
            detail = f"channel open · receivable (balance {balance_sat} sat)"
        elif channels:
            detail = f"channel(s) present but not usable yet (state: {', '.join(map(str, states))})"
        else:
            credit = f" {fee_credit_sat} sat held as fee credit;" if fee_credit_sat else ""
            detail = ("no inbound channel yet — can't receive payments." + credit
                      + " Open one with an initial inbound payment of ~25-35k sat (current fees).")
        return {"receivable": receivable, "detail": detail, "channels": len(channels),
                "balance_sat": balance_sat, "fee_credit_sat": fee_credit_sat}


def _nwc_run(coro_factory):
    """Run an NWC (async) call from this sync backend, reusing the CLIENT's async bridge
    (client.core._run_async) so we don't reimplement the threadpool/event-loop plumbing. A
    timeout keeps a dead relay from hanging the host. Imported lazily so mock/lnd/phoenixd never
    pull in nostr_sdk or the client package."""
    import asyncio
    from client.core import _run_async
    return _run_async(lambda: asyncio.wait_for(coro_factory(), 30.0))


def nwc_capability(uri: str) -> tuple[bool, str]:
    """Can this NWC wallet RECEIVE (advertise make_invoice)? Many wallets are pay-only and can't
    host. Returns (ok, message); the setup endpoint surfaces it to the picker.

    PRIMARY: read the wallet's PLAINTEXT kind-13194 NIP-47 info event off its relay (reusing the
    client's _nwc_fetch_info_event) — this avoids nostr-sdk's strict typed get_info deserialize,
    which rejects modern wallets that advertise newer methods/notifications (sign_message,
    payment_received, …) and would FALSELY reject a working receive-capable wallet.
    FALLBACK: typed get_info, where a deserialize / unknown-method error means the wallet RESPONDED
    (the SDK just couldn't map newer fields) — not a dead wallet. Only a real timeout = unavailable.
    """
    try:
        from nostr_sdk import NostrWalletConnectUri
        p = NostrWalletConnectUri.parse(uri)
        relays = [str(r) for r in p.relays()]
        wallet_pk = p.public_key().to_hex()
    except Exception as e:  # noqa: BLE001
        return False, f"invalid NWC connection string: {str(e)[:120]}"

    # PRIMARY — raw 13194 capabilities (plaintext; no strict typed parse)
    from client.core import _nwc_fetch_info_event
    info = _nwc_fetch_info_event(relays, wallet_pk)
    if info.get("found"):
        if "make_invoice" in info.get("methods", []):
            return True, "ok"
        return False, "this wallet can't receive over NWC (it doesn't advertise make_invoice)"

    # FALLBACK — typed get_info
    try:
        from nostr_sdk import Nwc, NostrWalletConnectUri as _Uri, Method
        client = Nwc(_Uri.parse(uri))  # same transport the client uses to pay
        resp = _nwc_run(client.get_info)
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        if any(s in detail.lower() for s in ("deserialize", "unknown method", "unknown notification")):
            # The wallet REPLIED; the SDK couldn't map newer fields and there's no 13194 to read.
            # Don't block a live wallet on a parse quirk — if it's truly pay-only, create_invoice
            # will fail clearly at first use.
            return True, "ok (wallet responded; capabilities unverified — SDK couldn't parse get_info)"
        return False, f"couldn't reach the NWC wallet: {str(e)[:140]}"
    if Method.MAKE_INVOICE not in resp.methods:
        return False, "this wallet can't receive over NWC (it doesn't support make_invoice)"
    return True, "ok"


class NwcLightning(LightningBackend):
    """Receive over Nostr Wallet Connect (NIP-47): the host is an NWC *client* against the
    operator's own wallet — issuing invoices with make_invoice and confirming them with
    lookup_invoice, instead of the client's pay_invoice. Reuses the exact same transport the client
    already uses to pay (nostr_sdk.Nwc + NostrWalletConnectUri), so nothing is reimplemented.

    Only as sovereign as the wallet: a custodial provider can freeze payouts or, with KYC, link the
    host. Many wallets are pay-only, so __init__ runs a capability guard and fails fast.
      NWC_URI  the wallet connection string (secret) — the same var the client uses to pay.
    """

    def __init__(self) -> None:
        uri = os.environ.get("NWC_URI", "").strip()
        if not uri:
            raise RuntimeError("PAYMENTS=nwc requires NWC_URI (the wallet connection string)")
        ok, detail = nwc_capability(uri)
        if not ok:
            raise RuntimeError(detail)
        from nostr_sdk import Nwc, NostrWalletConnectUri
        self._nwc = Nwc(NostrWalletConnectUri.parse(uri))  # persistent: reuses the relay connection

    def create_invoice(self, amount_msat: int, expiry_seconds: int | None = None) -> tuple[str, str]:
        from nostr_sdk import MakeInvoiceRequest
        req = MakeInvoiceRequest(amount=amount_msat, description="SAIL inference",
                                 description_hash=None, expiry=expiry_seconds)  # NIP-47 amount = msat
        try:
            resp = _nwc_run(lambda: self._nwc.make_invoice(req))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"NWC make_invoice failed: {e}") from e
        if not resp.payment_hash:
            raise RuntimeError("NWC wallet returned an invoice without a payment_hash")
        return resp.invoice, resp.payment_hash

    def is_settled(self, payment_hash_hex: str) -> bool:
        from nostr_sdk import LookupInvoiceRequest, TransactionState
        req = LookupInvoiceRequest(payment_hash=payment_hash_hex, invoice=None)
        try:
            resp = _nwc_run(lambda: self._nwc.lookup_invoice(req))
        except Exception:  # noqa: BLE001 — unknown hash / unreachable -> treat as unpaid
            return False
        return resp.state == TransactionState.SETTLED or resp.settled_at is not None

    def ping(self) -> tuple[bool, str]:
        from nostr_sdk import Method
        try:
            info = _nwc_run(self._nwc.get_info)
        except Exception as e:  # noqa: BLE001
            return False, f"NWC wallet unreachable: {str(e)[:80]}"
        return (Method.MAKE_INVOICE in info.methods, "NWC ok")


def get_backend() -> LightningBackend:
    kind = os.getenv("PAYMENTS", "mock").lower()
    if kind == "mock":
        return MockLightning()
    if kind == "lnd":
        return LndLightning()
    if kind == "phoenixd":
        return PhoenixdLightning()
    if kind == "nwc":
        return NwcLightning()
    raise ValueError(f"unknown PAYMENTS backend: {kind}")
