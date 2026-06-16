"""
Host daemon.

Serves an open model behind an L402 paywall, metered per output token, and publishes
its listing so clients can discover it. Phase 0 runs fully mocked:

    PAYMENTS=mock MODEL=mock uvicorn host.daemon:app --port 8001

Endpoints:
  GET  /v1/models      -> models this host offers
  POST /v1/inference   -> 402 (with macaroon+invoice) until paid; then streams tokens
  POST /mock/pay       -> PHASE 0 ONLY: simulate LN settling, reveal preimage to payer
"""
from __future__ import annotations

import os
import secrets

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from shared.config import load_env

load_env()  # before any module-level os.getenv below

from shared.l402 import L402Challenge, new_macaroon, parse_authorization, verify
from shared.listing import HostListing, ModelOffer
from shared import registry
from host import model as model_mod
from host import payments as pay_mod
from host import moderation

app = FastAPI(title="inference-net host")

# --- host identity & config -------------------------------------------------
PUBKEY = os.getenv("HOST_PUBKEY", "host_" + secrets.token_hex(8))
PORT = int(os.getenv("PORT", "8001"))
ENDPOINT = os.getenv("HOST_ENDPOINT", f"http://127.0.0.1:{PORT}")
PRICE_MSAT_PER_TOKEN = int(os.getenv("PRICE_MSAT_PER_TOKEN", "1000"))  # 1 sat/token (demo)

_model = model_mod.get_backend()
_ln = pay_mod.get_backend()

# macaroon -> (payment_hash, amount_msat); pending until paid
_pending: dict[str, tuple[str, int]] = {}


@app.on_event("startup")
def publish_listing() -> None:
    listing = HostListing(
        pubkey=PUBKEY,
        endpoint=ENDPOINT,
        models=[ModelOffer(name=_model.name, price_msat_per_token=PRICE_MSAT_PER_TOKEN,
                           context_window=8192)],
    )
    registry.publish(listing)  # Phase 1: publish signed Nostr event to relays
    print(f"[host] published listing: {PUBKEY} serving {_model.name} @ {ENDPOINT}")


@app.get("/v1/models")
def models() -> dict:
    return {"pubkey": PUBKEY,
            "models": [{"name": _model.name, "price_msat_per_token": PRICE_MSAT_PER_TOKEN}]}


@app.post("/v1/inference")
async def inference(request: Request, authorization: str | None = Header(default=None)):
    body = await request.json()
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 64))

    if not moderation.model_allowed(_model.name, allowlist=None):
        return JSONResponse({"error": "model not on network allowlist"}, status_code=451)

    creds = parse_authorization(authorization)

    # No valid creds -> issue an L402 challenge sized to the requested work.
    if creds is None:
        amount_msat = max_tokens * PRICE_MSAT_PER_TOKEN
        bolt11, payment_hash = _ln.create_invoice(amount_msat)
        macaroon = new_macaroon()
        _pending[macaroon] = (payment_hash, amount_msat)
        ch = L402Challenge(macaroon, bolt11, payment_hash, amount_msat)
        return JSONResponse(
            {"macaroon": macaroon, "invoice": bolt11,
             "payment_hash": payment_hash, "amount_msat": amount_msat},
            status_code=402,
            headers={"WWW-Authenticate": ch.www_authenticate()},
        )

    macaroon, preimage = creds
    bound = _pending.get(macaroon)
    if bound is None:
        return JSONResponse({"error": "unknown macaroon"}, status_code=402)
    payment_hash, amount_msat = bound
    if not verify(preimage, payment_hash):
        return JSONResponse({"error": "invalid preimage"}, status_code=402)

    # Paid. Stream tokens, metering as we go. Single-use macaroon for v0.
    del _pending[macaroon]

    def token_stream():
        spent = 0
        for i, tok in enumerate(_model.stream(prompt)):
            if i >= max_tokens:
                break
            spent += PRICE_MSAT_PER_TOKEN
            yield tok
        # trailer line the client can parse for the metered total
        yield f"\n__SPENT_MSAT__:{spent}"

    return StreamingResponse(token_stream(), media_type="text/plain")


# --- PHASE 0 ONLY -----------------------------------------------------------
@app.post("/mock/pay")
async def mock_pay(request: Request):
    """Simulate the LN network settling the invoice and revealing the preimage to the
    payer. Removed entirely once PAYMENTS=lnd; real payers learn the preimage by paying."""
    if os.getenv("PAYMENTS", "mock") != "mock":
        return JSONResponse({"error": "mock pay disabled"}, status_code=404)
    body = await request.json()
    preimage = _ln.reveal_preimage(body["payment_hash"])
    if preimage is None:
        return JSONResponse({"error": "unknown payment_hash"}, status_code=404)
    return {"preimage": preimage}
