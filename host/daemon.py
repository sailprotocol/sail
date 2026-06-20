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
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from shared.config import load_env

load_env()  # before any module-level os.getenv below

from shared.l402 import (
    L402Challenge, new_macaroon, parse_authorization, verify,
    next_trailer, done_trailer,
)
from shared.listing import HostListing, ModelOffer
from shared import registry
from host import model as model_mod
from host import payments as pay_mod
from host import moderation
from host import transport

app = FastAPI(title="SAIL host")

# --- host identity & config -------------------------------------------------
# With REGISTRY=nostr the listing identity IS the host's Nostr pubkey; otherwise (local)
# fall back to HOST_PUBKEY or a random dev identity.
PUBKEY = registry.host_identity() or os.getenv("HOST_PUBKEY", "host_" + secrets.token_hex(8))
PORT = int(os.getenv("PORT", "8001"))
ENDPOINT = os.getenv("HOST_ENDPOINT", f"http://127.0.0.1:{PORT}")
PRICE_MSAT_PER_TOKEN = int(os.getenv("PRICE_MSAT_PER_TOKEN", "1000"))  # 1 sat/token (demo)
TRANSPORT = os.getenv("TRANSPORT", "clearnet").lower()  # clearnet | tor
CHUNK_TOKENS = max(1, int(os.getenv("CHUNK_TOKENS", "8")))  # metered settlement granularity
SESSION_TTL = 300  # seconds; evict abandoned generation sessions

_model = model_mod.get_backend()
_ln = pay_mod.get_backend()

# Metered settlement: generation runs in chunks, each chunk paid via its own L402 challenge,
# so the client pays for tokens actually delivered (overpay bounded to < 1 chunk) instead of
# prepaying max_tokens. State is per-generation and ephemeral — nothing persists across requests.
#
# macaroon -> (session_id, payment_hash, amount_msat, chunk_tokens); single-use, deleted on pay
_pending: dict[str, tuple[str, str, int, int]] = {}
# session_id -> live generation state for one streamed response
_sessions: dict[str, dict] = {}
_NOTHING = object()  # 1-token look-ahead sentinel (distinguishes "no buffered token")


def _evict_stale() -> None:
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v["created"] > SESSION_TTL]:
        gen = _sessions[sid].get("gen")
        if gen is not None:
            try:
                gen.close()
            except Exception:
                pass
        _sessions.pop(sid, None)


def _issue_chunk_challenge(sid: str) -> dict:
    """Invoice the next chunk for a session and register a single-use macaroon."""
    s = _sessions[sid]
    chunk = min(s["chunk_tokens"], s["max_tokens"] - s["emitted"])
    amount_msat = chunk * PRICE_MSAT_PER_TOKEN
    bolt11, payment_hash = _ln.create_invoice(amount_msat)
    macaroon = new_macaroon()
    _pending[macaroon] = (sid, payment_hash, amount_msat, chunk)
    s["charged_msat"] += amount_msat
    return {"session_id": sid, "macaroon": macaroon, "invoice": bolt11,
            "payment_hash": payment_hash, "amount_msat": amount_msat}


@app.on_event("startup")
def publish_listing() -> None:
    global ENDPOINT
    # Moderation gate: refuse to start serving a disallowed model, or any image-modality model
    # without a real CSAM matcher. Fail loudly here rather than per-request.
    moderation.assert_can_serve(_model)
    if TRANSPORT == "tor":
        # Expose the daemon as a .onion and advertise THAT as the endpoint.
        ENDPOINT = transport.setup_onion(PORT)
        print(f"[host] tor onion endpoint: {ENDPOINT}")
    listing = HostListing(
        pubkey=PUBKEY,
        endpoint=ENDPOINT,
        models=[ModelOffer(name=_model.name, price_msat_per_token=PRICE_MSAT_PER_TOKEN,
                           context_window=8192, modality=_model.modality)],
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

    if not moderation.is_model_allowed(_model.name):
        return JSONResponse({"error": "model not on network allowlist"}, status_code=451)

    creds = parse_authorization(authorization)

    # No valid creds -> start a session and issue the FIRST chunk's L402 challenge.
    # No generation happens yet, so an unpaid request never costs the host compute.
    if creds is None:
        _evict_stale()
        sid = secrets.token_hex(8)
        chunk_tokens = max(1, int(body.get("chunk_tokens", CHUNK_TOKENS)))
        _sessions[sid] = {
            "prompt": prompt, "max_tokens": max_tokens, "chunk_tokens": chunk_tokens,
            "gen": None, "buf": _NOTHING, "emitted": 0, "charged_msat": 0,
            "created": time.time(),
        }
        ch = _issue_chunk_challenge(sid)
        l402 = L402Challenge(ch["macaroon"], ch["invoice"], ch["payment_hash"], ch["amount_msat"])
        return JSONResponse(ch, status_code=402,
                            headers={"WWW-Authenticate": l402.www_authenticate()})

    # Valid creds -> redeem one paid chunk: verify, stream up to chunk tokens, then either
    # hand back the next chunk's challenge (__L402_NEXT__) or finish (__L402_DONE__).
    macaroon, preimage = creds
    bound = _pending.get(macaroon)
    if bound is None:
        return JSONResponse({"error": "unknown macaroon"}, status_code=402)
    sid, payment_hash, amount_msat, chunk = bound
    if not verify(preimage, payment_hash):
        return JSONResponse({"error": "invalid preimage"}, status_code=402)
    del _pending[macaroon]  # single-use
    s = _sessions.get(sid)
    if s is None:
        return JSONResponse({"error": "unknown or expired session"}, status_code=410)

    def chunk_stream():
        if s["gen"] is None:
            s["gen"] = _model.stream(s["prompt"])  # lazy: only after first payment
        gen = s["gen"]

        def _next_token():
            if s["buf"] is not _NOTHING:  # deliver the look-ahead token first
                tok, s["buf"] = s["buf"], _NOTHING
                return tok
            return next(gen)  # raises StopIteration when the model is done

        allow = min(chunk, s["max_tokens"] - s["emitted"])
        exhausted = False
        for _ in range(allow):
            try:
                tok = _next_token()
            except StopIteration:
                exhausted = True
                break
            s["emitted"] += 1
            yield tok

        # Look ahead one token: only invoice another chunk if more output actually exists
        # (and we're under max_tokens) — this avoids billing an empty trailing chunk.
        more = False
        if not exhausted and s["emitted"] < s["max_tokens"]:
            try:
                s["buf"] = next(gen)
                more = True
            except StopIteration:
                s["buf"] = _NOTHING

        if more:
            yield next_trailer(_issue_chunk_challenge(sid))
        else:
            spent = s["charged_msat"]
            try:
                gen.close()
            except Exception:
                pass
            _sessions.pop(sid, None)
            yield done_trailer(spent)

    return StreamingResponse(chunk_stream(), media_type="text/plain")


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
