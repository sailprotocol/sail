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

import collections
import os
import pathlib
import secrets
import threading
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from shared.config import load_env

load_env()  # before any module-level os.getenv below

from shared.l402 import (
    L402Challenge, new_macaroon, parse_authorization, verify,
    next_trailer, done_trailer, error_trailer,
)
from shared.alias import derive_alias, alias_label
from shared.listing import HostListing, ModelOffer
from shared import registry
from host import model as model_mod
from host import payments as pay_mod
from host import moderation
from host import transport

# Two ASGI apps on two binds, so the operator surface is PHYSICALLY unreachable over Tor — not
# just header-gated. `app` is PUBLIC: only /v1/* (paid inference) + /mock/* (dev), and it's the
# one the Tor hidden service forwards to (transport.setup_onion → PORT). `operator_app` is the
# operator surface (dashboard, wizard, /api/wallet/*, /api/setup/*, /api/control/*) and binds
# LOCALHOST-ONLY (OPERATOR_HOST:OPERATOR_PORT), never added to the onion config. So the seed /
# pay / close endpoints can't be reached over the onion regardless of the Host header.
app = FastAPI(title="SAIL host")
operator_app = FastAPI(title="SAIL host — operator (local only)")

# --- host identity & config -------------------------------------------------
# Stable, persisted Nostr identity (shared/identity.py) — same real pubkey + alias across reboots
# in EVERY mode (clearnet/local included), generated + persisted on first run. No throwaway keys.
from shared import identity
PUBKEY = identity.host_pubkey_hex()
PORT = int(os.getenv("PORT", "8001"))
ENDPOINT = os.getenv("HOST_ENDPOINT", f"http://127.0.0.1:{PORT}")
# Operator surface: a separate localhost-only listener (NEVER added to the Tor hidden service).
OPERATOR_HOST = os.getenv("OPERATOR_HOST", "127.0.0.1")  # keep it loopback — do not bind 0.0.0.0
OPERATOR_PORT = int(os.getenv("OPERATOR_PORT", "8090"))
PRICE_MSAT_PER_TOKEN = int(os.getenv("PRICE_MSAT_PER_TOKEN", "1000"))  # 1 sat/token (demo)
TRANSPORT = os.getenv("TRANSPORT", "clearnet").lower()  # clearnet | tor
CHUNK_TOKENS = max(1, int(os.getenv("CHUNK_TOKENS", "8")))  # metered settlement granularity
SESSION_TTL = 300  # seconds; evict abandoned generation sessions
BOLT11_EXPIRY_SECONDS = int(os.getenv("BOLT11_EXPIRY_SECONDS", "600"))  # manual-pay window

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

# Manual BOLT11 fallback: ONE invoice for the ceiling (max_tokens × price), paid from any wallet;
# the host confirms settlement via its own LND and then streams. session_id is the bearer token.
# sid -> {prompt, max_tokens, payment_hash, amount_msat, created, served}
_bolt11: dict[str, dict] = {}

# --- read-only metrics for the operator dashboard (in-memory; reset on restart) -------------
_START_TIME = time.time()
_stats = {"day": "", "tokens": 0, "msat": 0}      # tokens served + msat earned today
_activity: collections.deque = collections.deque(maxlen=20)  # recent completed sessions
_STATIC = pathlib.Path(__file__).parent / "static"


def _record_settled(tag: str, tokens: int, msat: int) -> None:
    """Tally a completed session into today's totals + the recent-activity ring."""
    today = time.strftime("%Y-%m-%d", time.localtime())
    if _stats["day"] != today:
        _stats.update(day=today, tokens=0, msat=0)
    _stats["tokens"] += tokens
    _stats["msat"] += msat
    _activity.appendleft({"tag": tag, "model": _model.name, "tokens": tokens,
                          "msat": msat, "state": "settled", "ts": int(time.time())})


def _today() -> dict:
    """Today's totals, rolling over at the date boundary."""
    if _stats["day"] != time.strftime("%Y-%m-%d", time.localtime()):
        _stats.update(day=time.strftime("%Y-%m-%d", time.localtime()), tokens=0, msat=0)
    return _stats


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


# How often to re-announce the listing so it stays fresh on public relays (set 0 to disable).
REANNOUNCE_SECONDS = int(os.getenv("LISTING_REANNOUNCE_SECONDS", "300"))


def _live_to_serve() -> bool:
    """True once a real payout backend is configured. A fresh host in the setup wizard runs with
    PAYMENTS=mock and is NOT yet serving paid inference; go-live writes a real backend
    (phoenixd/lnd/nwc) and restarts the daemon, at which point this flips true."""
    return os.getenv("PAYMENTS", "mock").lower() != "mock"


def _public_publish_withheld() -> str | None:
    """Reason to withhold a PUBLIC-relay announce (else None to publish).

    Don't leak a discoverable kind-38111 listing to public Nostr relays until the host is actually
    live-to-serve — otherwise every aborted/half-finished/test wizard run litters discovery with
    ghost hosts that can't serve. The LOCAL registry (dev/test) is never withheld."""
    if os.getenv("REGISTRY", "local").lower() == "nostr" and not _live_to_serve():
        return ("still in setup (PAYMENTS=mock) — not announcing to public relays yet; "
                "complete go-live in the wizard to publish your listing")
    return None


def _build_listing() -> HostListing:
    return HostListing(
        pubkey=PUBKEY,
        endpoint=ENDPOINT,
        models=[ModelOffer(name=_model.name, price_msat_per_token=PRICE_MSAT_PER_TOKEN,
                           context_window=8192, modality=_model.modality)],
    )


def _log_publish(result: dict, label: str) -> None:
    """Surface the per-relay outcome. A PARTIAL publish (at least one relay accepted, others timed
    out) is normal on public relays — lead with the success and present the stragglers as a soft
    retry note, not a scary REJECTED. Only a publish where ZERO relays accepted is a real failure."""
    ok = result.get("success", []) if result else []
    failed = result.get("failed", {}) if result else {}
    total = len(ok) + len(failed)
    if ok:
        print(f"[host] {label}: published to {len(ok)} of {total} relay(s): {', '.join(ok)}")
        if failed:
            detail = "; ".join(f"{r}: {why}" for r, why in failed.items())
            print(f"[host] {label}: {len(failed)} of {total} relay(s) didn't accept this round "
                  f"({detail}) — normal, will retry on the next heartbeat")
    else:
        detail = "; ".join(f"{r}: {why}" for r, why in failed.items()) if failed else "no relays configured"
        print(f"[host] WARNING: {label}: NO relay accepted the listing — it won't be discoverable "
              f"({detail}). Check outbound connectivity to the relays and this host's clock.")


def _reannounce_loop(interval: int) -> None:
    """Re-publish the listing periodically. Kind-38111 is a parameterized-replaceable event (stable
    'd' tag = pubkey), so relays keep/replace the latest — but public relays still drop events over
    time, so a heartbeat keeps the host discoverable instead of vanishing between client queries."""
    while True:
        time.sleep(interval)
        if _public_publish_withheld():  # same gate as startup: never heartbeat a not-yet-live host
            continue
        try:
            _log_publish(registry.publish(_build_listing()), f"re-announce ({alias_label(PUBKEY)})")
        except Exception as e:  # noqa: BLE001 — keep the host serving even if a relay hiccups
            print(f"[host] re-announce failed: {e}")


def _run_operator_app() -> None:
    """Serve the operator surface on a LOCALHOST-ONLY bind that is never added to the Tor hidden
    service — so the dashboard, wizard, and /api/wallet/* (seed, pay, close) + /api/setup/* are
    physically unreachable over the onion, not merely header-gated."""
    import socket
    import uvicorn
    # Pre-flight the bind: if the operator port is taken, uvicorn would just log a terse errno and
    # let the thread die — say so clearly instead. Inference keeps serving regardless.
    try:
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((OPERATOR_HOST, OPERATOR_PORT))
        probe.close()
    except OSError as e:
        print(f"[host] WARNING: operator surface can't bind {OPERATOR_HOST}:{OPERATOR_PORT} ({e}). "
              f"Dashboard/wizard/wallet are unavailable — set OPERATOR_PORT to a free port and "
              f"restart. (Inference is unaffected.)")
        return
    cfg = uvicorn.Config(operator_app, host=OPERATOR_HOST, port=OPERATOR_PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None  # we're not the main thread
    server.run()


def _maybe_start_operator() -> None:
    """Start the operator listener once, in a daemon thread. Skipped under tests (TestClient fires
    startup events) via SAIL_OPERATOR_AUTOSTART=0 so the suite doesn't bind a real port."""
    if os.getenv("SAIL_OPERATOR_AUTOSTART", "1") == "0":
        return
    threading.Thread(target=_run_operator_app, daemon=True, name="sail-operator").start()
    print(f"[host] operator surface (LOCAL ONLY): http://{OPERATOR_HOST}:{OPERATOR_PORT}/  "
          f"— dashboard, wizard, wallet. Not exposed over Tor.")


@app.on_event("startup")
def publish_listing() -> None:
    global ENDPOINT
    # Bring up the operator surface first, so the dashboard/wizard are reachable even before (or
    # without) a public listing — e.g. a fresh host still in /setup.
    _maybe_start_operator()
    # phoenixd seed.dat + phoenix.conf hold plaintext recovery material; make sure they're 0600
    # (backstop for nodes provisioned before SAIL tightened perms — see phoenixd_setup).
    if os.getenv("PAYMENTS", "mock").lower() == "phoenixd":
        try:
            from host import phoenixd_setup
            phoenixd_setup.secure_seed_files()
        except Exception as e:  # noqa: BLE001 — never block startup on a perms tidy
            print(f"[host] note: could not tighten ~/.phoenix perms: {e}")
    # Moderation gate: refuse to start serving a disallowed model, or any image-modality model
    # without a real CSAM matcher. Fail loudly here rather than per-request.
    moderation.assert_can_serve(_model)
    # PoW floor: if we mine below what clients require, they silently filter us out.
    pow_target = int(os.getenv("POW_TARGET", "16"))
    pow_min = int(os.getenv("POW_MIN_DIFFICULTY", "8"))
    if pow_target < pow_min:
        print(f"[host] WARNING: POW_TARGET ({pow_target}) < client POW_MIN_DIFFICULTY ({pow_min}) "
              f"— clients will reject this listing. Raise POW_TARGET.")
    # Tiny per-chunk amounts are hard to pay: some wallets reject sub-~10-sat invoices, and a fresh
    # phoenixd with no inbound channel can't receive ANY amount until one is bootstrapped (~25-35k).
    chunk_msat = PRICE_MSAT_PER_TOKEN * CHUNK_TOKENS
    if chunk_msat < 10_000:
        print(f"[host] NOTE: per-chunk payment is {chunk_msat // 1000} sat "
              f"({PRICE_MSAT_PER_TOKEN}msat × {CHUNK_TOKENS} tokens). NWC/Lightning payers may "
              f"reject very small invoices; consider a larger CHUNK_TOKENS. On phoenixd you also "
              f"can't receive until an initial inbound payment opens a channel (~25-35k sat).")
    if TRANSPORT == "tor":
        # Expose the daemon as a .onion and advertise THAT as the endpoint.
        ENDPOINT = transport.setup_onion(PORT)
        print(f"[host] tor onion endpoint: {ENDPOINT}")
    # F11: don't announce to PUBLIC relays until the host is live-to-serve (post go-live). A fresh
    # host still in the wizard would otherwise leak a ghost listing that can't serve.
    withheld = _public_publish_withheld()
    if withheld:
        print(f"[host] {withheld}")
        return
    result = registry.publish(_build_listing())  # signed, PoW-mined, parameterized-replaceable
    print(f"[host] published listing: {alias_label(PUBKEY)} [{PUBKEY}] "
          f"serving {_model.name} @ {ENDPOINT}")
    _log_publish(result, "publish")  # show which relays actually accepted (or that none did)
    if REANNOUNCE_SECONDS > 0:
        threading.Thread(target=_reannounce_loop, args=(REANNOUNCE_SECONDS,),
                         daemon=True, name="sail-reannounce").start()
        print(f"[host] re-announce heartbeat every {REANNOUNCE_SECONDS}s")


@app.get("/v1/models")
def models() -> dict:
    return {"pubkey": PUBKEY,
            "models": [{"name": _model.name, "price_msat_per_token": PRICE_MSAT_PER_TOKEN}]}


@app.post("/v1/inference")
async def inference(request: Request, authorization: str | None = Header(default=None)):
    # Guard the body: junk/empty input must be a clean 400, never a 500 stack trace (a 500 on
    # garbage is a cheap DoS surface). A well-formed body still flows on to the 402 challenge.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — any JSON decode failure (empty/malformed) is a bad request
        return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    prompt = body.get("prompt", "")
    try:
        max_tokens = int(body.get("max_tokens", 64))
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)

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
        print(f"[host] L402 challenge issued sid={sid} hash={ch['payment_hash'][:8]} "
              f"amount={ch['amount_msat']}msat mac={ch['macaroon'][:8]}")
        return JSONResponse(ch, status_code=402,
                            headers={"WWW-Authenticate": l402.www_authenticate()})

    # Valid creds -> redeem one paid chunk: verify, stream up to chunk tokens, then either
    # hand back the next chunk's challenge (__L402_NEXT__) or finish (__L402_DONE__).
    macaroon, preimage = creds
    print(f"[host] L402 paid-request received mac={macaroon[:8]} preimage_len={len(preimage or '')}")
    bound = _pending.get(macaroon)
    if bound is None:
        print(f"[host] L402 REJECT unknown/expired macaroon mac={macaroon[:8]}")
        return JSONResponse({"error": "unknown macaroon"}, status_code=402)
    sid, payment_hash, amount_msat, chunk = bound
    if not verify(preimage, payment_hash):
        print(f"[host] L402 REJECT invalid preimage hash={payment_hash[:8]} mac={macaroon[:8]}")
        return JSONResponse({"error": "invalid preimage"}, status_code=402)
    del _pending[macaroon]  # single-use
    s = _sessions.get(sid)
    if s is None:
        print(f"[host] L402 REJECT session expired sid={sid} mac={macaroon[:8]}")
        return JSONResponse({"error": "unknown or expired session"}, status_code=410)
    print(f"[host] L402 verified -> serving sid={sid} chunk={chunk}tok emitted={s['emitted']}")

    def chunk_stream():
        # phase tracks WHERE we are if serving aborts, so the host log pinpoints the cause
        # (model cold-load vs mid-stream read vs issuing the next chunk's invoice).
        phase = "start"
        try:
            if s["gen"] is None:
                phase = "model_open"
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
                phase = "model_read"
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
                phase = "model_lookahead"
                try:
                    s["buf"] = next(gen)
                    more = True
                except StopIteration:
                    s["buf"] = _NOTHING

            if more:
                phase = "issue_invoice"  # phoenixd/LND/NWC mint for the NEXT chunk
                trailer = next_trailer(_issue_chunk_challenge(sid))
                yield trailer
            else:
                spent = s["charged_msat"]
                try:
                    gen.close()
                except Exception:
                    pass
                _record_settled(sid[:4], s["emitted"], spent)
                _sessions.pop(sid, None)
                yield done_trailer(spent)
        except Exception as e:  # noqa: BLE001 — pay-then-fail: end cleanly, don't cut the stream
            spent = s.get("charged_msat", 0)      # what the client has paid (in-flight chunk only)
            delivered = s.get("emitted", 0)
            print(f"[host] serve ABORTED sid={sid[:4]} phase={phase} delivered={delivered}tok "
                  f"spent={spent}msat: {type(e).__name__}: {str(e)[:200]}")
            try:
                if s.get("gen"):
                    s["gen"].close()
            except Exception:
                pass
            # Stop here: no further chunk is charged. We can't auto-refund the in-flight chunk over
            # LN in v1, so we report exactly what was spent vs delivered (loss bounded to <1 chunk).
            _record_settled(sid[:4], delivered, spent)
            _sessions.pop(sid, None)
            yield error_trailer(message="host failed to serve the model (stream ended early)",
                                spent_msat=spent, delivered_tokens=delivered,
                                reason=f"{phase}:{type(e).__name__}")

    return StreamingResponse(chunk_stream(), media_type="text/plain")


# --- Manual BOLT11 fallback (pay from any Lightning wallet) ------------------
def _bolt11_state(s: dict) -> str:
    if s.get("settled") or _ln.is_settled(s["payment_hash"]):
        s["settled"] = True
        return "settled"
    if time.time() - s["created"] > BOLT11_EXPIRY_SECONDS:
        return "expired"
    return "waiting"


@app.post("/v1/inference/bolt11")
async def bolt11_create(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty/malformed body -> 400, same public DoS guard as /v1/inference
        return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    prompt = body.get("prompt", "")
    try:
        max_tokens = int(body.get("max_tokens", 64)) or 64
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)
    if not moderation.is_model_allowed(_model.name):
        return JSONResponse({"error": "model not on network allowlist"}, status_code=451)
    # Evict expired bolt11 sessions opportunistically.
    for sid in [k for k, v in _bolt11.items()
                if time.time() - v["created"] > BOLT11_EXPIRY_SECONDS and not v.get("served")]:
        _bolt11.pop(sid, None)
    amount_msat = max_tokens * PRICE_MSAT_PER_TOKEN  # the ceiling — coarse, no refund (v1)
    # The invoice's OWN expiry must match the session window, so a wallet can't pay it after the
    # host has evicted the session (which would take funds with no service).
    bolt11, payment_hash = _ln.create_invoice(amount_msat, expiry_seconds=BOLT11_EXPIRY_SECONDS)
    sid = secrets.token_hex(16)
    _bolt11[sid] = {"prompt": prompt, "max_tokens": max_tokens, "payment_hash": payment_hash,
                    "amount_msat": amount_msat, "created": time.time(), "served": False}
    return {"session_id": sid, "invoice": bolt11, "payment_hash": payment_hash,
            "amount_msat": amount_msat, "expires_in": BOLT11_EXPIRY_SECONDS}


@app.get("/v1/inference/bolt11/{sid}/status")
def bolt11_status(sid: str):
    s = _bolt11.get(sid)
    if s is None:
        return JSONResponse({"error": "unknown or expired session"}, status_code=404)
    state = _bolt11_state(s)
    if state == "expired":
        _bolt11.pop(sid, None)
    return {"state": state, "amount_msat": s["amount_msat"]}


@app.post("/v1/inference/bolt11/{sid}/stream")
def bolt11_stream(sid: str):
    s = _bolt11.get(sid)
    if s is None:
        return JSONResponse({"error": "unknown or expired session"}, status_code=404)
    state = _bolt11_state(s)
    if state == "expired":
        _bolt11.pop(sid, None)
        return JSONResponse({"error": "invoice expired"}, status_code=410)
    if state != "settled":
        return JSONResponse({"error": "invoice not paid yet"}, status_code=402)
    if s.get("served"):
        return JSONResponse({"error": "session already served"}, status_code=409)
    s["served"] = True

    def token_stream():
        n = 0
        for i, tok in enumerate(_model.stream(s["prompt"])):
            if i >= s["max_tokens"]:
                break
            n += 1
            yield tok
        _record_settled(sid[:4], n, s["amount_msat"])
        _bolt11.pop(sid, None)
        yield done_trailer(s["amount_msat"])  # manual prepay = the ceiling

    return StreamingResponse(token_stream(), media_type="text/plain")


# --- PHASE 0 ONLY -----------------------------------------------------------
@app.post("/mock/settle")
async def mock_settle(request: Request):
    """Simulate a foreign wallet paying a BOLT11 invoice (so is_settled() flips). PAYMENTS=mock only."""
    if os.getenv("PAYMENTS", "mock") != "mock":
        return JSONResponse({"error": "mock settle disabled"}, status_code=404)
    body = await request.json()
    _ln.mark_settled(body["payment_hash"])
    return {"settled": True}


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


# --- operator dashboard (LOCAL ONLY) ----------------------------------------
# The daemon's inference routes are exposed over the .onion (onion:80 -> 127.0.0.1:PORT), so
# anything served here is reachable over Tor too. The dashboard + status report earnings/activity,
# which are operator-private — so gate them to local access only (request Host is not the .onion).
# Best-effort (the Host header is client-supplied); a dedicated localhost-only port is the harden.
def _is_local(request: Request) -> bool:
    return ".onion" not in (request.headers.get("host", ""))


@operator_app.get("/sail.css")
def sail_css():
    """Shared SAIL stylesheet (design tokens + components), used by the dashboard + wizard. Static
    design assets only — no data — so it's served ungated."""
    return FileResponse(_STATIC / "sail.css", media_type="text/css")


@operator_app.get("/")
def dashboard(request: Request):
    if not _is_local(request):
        return JSONResponse({"error": "not found"}, status_code=404)  # don't expose over the onion
    return FileResponse(_STATIC / "dashboard.html")


_PAY_HEALTH_TTL = 30  # seconds; payment-API pings are cached so /api/status polling stays cheap
_pay_health = {"ts": 0.0, "ok": None, "detail": "", "receivable": True, "receive_detail": "ok"}


def _payments_health() -> dict:
    """Cached payment-backend health: API reachable (ping) AND able to receive (receive_status —
    the phoenixd channel-cliff guard). Refuse to declare a host 'live to earn' when either fails."""
    now = time.time()
    if _pay_health["ok"] is None or now - _pay_health["ts"] > _PAY_HEALTH_TTL:
        try:
            ok, detail = _ln.ping()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)[:120]
        try:
            rs = _ln.receive_status()
            receivable, receive_detail = rs.get("receivable"), rs.get("detail", "")
        except Exception as e:  # noqa: BLE001
            receivable, receive_detail = None, str(e)[:120]
        _pay_health.update(ts=now, ok=bool(ok), detail=detail,
                           receivable=receivable, receive_detail=receive_detail)
    return _pay_health


@operator_app.get("/api/status")
def api_status(request: Request):
    if not _is_local(request):
        return JSONResponse({"error": "not found"}, status_code=404)
    t = _today()
    _h = _payments_health()
    pay_ok, pay_detail = _h["ok"], _h["detail"]
    # in-progress metered generations (started, not yet done) shown as "streaming"
    streaming = [{"tag": sid[:4], "model": _model.name, "tokens": v["emitted"],
                  "msat": v["charged_msat"], "state": "streaming"}
                 for sid, v in _sessions.items() if v.get("gen") is not None]
    return {
        "state": "live",
        "pubkey": PUBKEY,
        "alias": derive_alias(PUBKEY),
        "alias_label": alias_label(PUBKEY),
        "onion": ENDPOINT,
        "transport": TRANSPORT,
        "model": _model.name,
        "payments": os.getenv("PAYMENTS", "mock").lower(),
        "payments_ready": pay_ok,        # payment backend API is responding (host is payable)
        "payments_detail": pay_detail,
        "receivable": _h["receivable"],          # can actually RECEIVE (phoenixd channel-cliff guard)
        "receive_detail": _h["receive_detail"],
        "price_msat_per_token": PRICE_MSAT_PER_TOKEN,
        "chunk_tokens": CHUNK_TOKENS,
        "invoice_expiry_s": BOLT11_EXPIRY_SECONDS,
        "uptime_s": int(time.time() - _START_TIME),
        "streaming_now": len(streaming),
        "tokens_today": t["tokens"],
        "sats_today": t["msat"] // 1000,
        "activity": streaming + list(_activity),
    }


@operator_app.get("/api/selftest")
def api_selftest(request: Request):
    """Self-test: send a tiny prompt to our OWN endpoint (over Tor if it's a .onion) and confirm a
    402 + invoice comes back — proof clients can reach us and pay. Mints one unpaid invoice."""
    if not _is_local(request):
        return JSONResponse({"error": "not found"}, status_code=404)
    import httpx
    via = "tor" if ".onion" in ENDPOINT else "direct"
    proxy = os.getenv("TOR_SOCKS", "socks5h://127.0.0.1:9050") if via == "tor" else None
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{ENDPOINT}/v1/inference", json={"prompt": "selftest", "max_tokens": 1},
                       proxy=proxy, timeout=45.0)
        ok = r.status_code == 402 and "invoice" in r.text
        return {"ok": ok, "status": r.status_code, "via": via,
                "latency_ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "via": via, "error": str(e)[:140]}


# --- host wallet (LOCAL ONLY — moves money) ---------------------------------
# Wraps phoenixd's HTTP API so the operator can see + move sats from the dashboard instead of
# terminal phoenixd commands. These move money, so they share the dashboard's local-only gate
# (never reachable over the onion) and are phoenixd-specific (lnd/nwc operators manage their own
# node/wallet elsewhere).
def _wallet_gate(request: Request):
    """Gate every wallet route: local-only (NEVER over the onion — these move money / reveal the
    seed) AND phoenixd-only. Returns a JSONResponse to short-circuit, or None when allowed."""
    if not _is_local(request):
        return _NOT_FOUND  # don't expose the wallet over the onion
    if os.getenv("PAYMENTS", "mock").lower() != "phoenixd":
        return JSONResponse(
            {"error": "the wallet is available only with the phoenixd payout backend"},
            status_code=400)
    return None


def _wallet_or_error(request: Request):
    """Return (wallet, None) when allowed, else (None, JSONResponse) with the reason."""
    err = _wallet_gate(request)
    if err is not None:
        return None, err
    from host import wallet
    return wallet.get_wallet(), None


def _wallet_call(request: Request, fn):
    """Run a wallet call behind the gate, surfacing phoenixd failures as a clean 502."""
    from host import wallet
    w, err = _wallet_or_error(request)
    if err is not None:
        return err
    try:
        return fn(w)
    except wallet.WalletError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@operator_app.get("/api/wallet/balance")
def wallet_balance(request: Request):
    return _wallet_call(request, lambda w: w.balance())


@operator_app.get("/api/wallet/channels")
def wallet_channels(request: Request):
    return _wallet_call(request, lambda w: w.channels())


def _qr_data_uri(text: str) -> str | None:
    """Render a BOLT11 as an inline SVG data-URI QR with segno (already a repo dep). Generated
    LOCALLY — no external QR service — so the wallet stays sovereign. Black-on-white for scanners."""
    if not text:
        return None
    import segno
    return segno.make(text, error="l").svg_data_uri(scale=4, border=3, dark="#000000", light="#ffffff")


@operator_app.post("/api/wallet/receive")
async def wallet_receive(request: Request):
    """Mint a BOLT11 to receive into the wallet (the 'fund me / auto-open my channel' invoice).
    amountSat optional (blank = any-amount). Returns the invoice as copyable text + an inline QR."""
    from host import wallet
    w, err = _wallet_or_error(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty/invalid body is fine; treat as no-amount invoice
        body = {}
    amount = body.get("amountSat")
    if amount in ("", None):
        amount = None
    else:
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return JSONResponse({"error": "amountSat must be a whole number of sats"}, status_code=400)
        if amount < 1:
            return JSONResponse({"error": "amountSat must be at least 1 sat"}, status_code=400)
    description = (body.get("description") or "").strip() or None
    try:
        result = w.receive(amount_sat=amount, description=description)
    except wallet.WalletError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    result["qr"] = _qr_data_uri(result.get("bolt11"))
    return result


@operator_app.get("/api/wallet/incoming/{payment_hash}")
def wallet_incoming(payment_hash: str, request: Request):
    """Has THIS invoice been paid? Backs the receive modal's 'payment received' confirmation so the
    operator gets explicit feedback (not just a background balance change)."""
    return _wallet_call(request, lambda w: w.incoming_status(payment_hash))


@operator_app.post("/api/wallet/seed")
async def wallet_seed(request: Request):
    """Reveal the phoenixd recovery seed for the operator to back up. EXTREMELY sensitive: anyone
    with these words controls the funds. Defenses: the local-only gate (never the onion), POST +
    an explicit {"confirm":"reveal"} so it can't be triggered accidentally / by a prefetch, and the
    words are NEVER logged or persisted by SAIL — read straight from phoenixd's seed file and
    returned only in this response body."""
    err = _wallet_gate(request)  # local-only + phoenixd; no phoenixd client needed to read the file
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if (body.get("confirm") or "") != "reveal":
        return JSONResponse({"error": "explicit confirmation required"}, status_code=400)
    from host import phoenixd_setup
    try:
        words = phoenixd_setup.read_seed_words()
    except FileNotFoundError:
        return JSONResponse({"error": "no phoenixd seed file found on this host"}, status_code=404)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:160]}, status_code=500)
    return {"words": words, "count": len(words)}


@operator_app.post("/api/wallet/pay")
async def wallet_pay(request: Request):
    """Withdraw via Lightning: pay an external BOLT11 from the wallet. phoenixd /payinvoice."""
    from host import wallet
    w, err = _wallet_or_error(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    invoice = (body.get("invoice") or "").strip()
    if not invoice:
        return JSONResponse({"error": "a Lightning invoice (BOLT11) is required"}, status_code=400)
    amount = body.get("amountSat")
    if amount in ("", None):
        amount = None
    else:
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return JSONResponse({"error": "amountSat must be a whole number of sats"}, status_code=400)
        if amount < 1:
            return JSONResponse({"error": "amountSat must be at least 1 sat"}, status_code=400)
    try:
        return w.pay(invoice, amount_sat=amount)
    except wallet.WalletError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@operator_app.post("/api/wallet/close")
async def wallet_close(request: Request):
    """Close the channel(s) and sweep the on-chain remainder to the operator's BTC address. The
    Lightning balance should be withdrawn first (pay); this empties what's left on-chain."""
    from host import wallet
    w, err = _wallet_or_error(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    address = (body.get("address") or "").strip()
    if not address:
        return JSONResponse({"error": "a destination BTC address is required"}, status_code=400)
    try:
        feerate = int(body.get("feerateSatByte"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "feerateSatByte must be a whole number (sat/vByte)"}, status_code=400)
    if feerate < 1:
        return JSONResponse({"error": "feerateSatByte must be at least 1"}, status_code=400)
    try:
        # Close the specific channel if given, else every open channel (sweep everything out).
        cid = (body.get("channelId") or "").strip()
        ids = [cid] if cid else [c["channelId"] for c in w.channels()["channels"] if c.get("channelId")]
        if not ids:
            return JSONResponse({"error": "no channel to close"}, status_code=400)
        closed = [w.close(c, address, feerate) for c in ids]
    except wallet.WalletError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"closed": closed, "count": len(closed)}


# --- first-run setup wizard (LOCAL ONLY) ------------------------------------
# Same local-only gate as the dashboard: these read hardware/config and WRITE .env.host + restart
# the service, so they must never be reachable over the .onion. The wizard is host/static/wizard.html.
_NOT_FOUND = JSONResponse({"error": "not found"}, status_code=404)


def _ollama_models() -> list[dict]:
    """Installed Ollama models (name + size), or [] if Ollama isn't reachable."""
    import httpx
    url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags"
    try:
        r = httpx.get(url, timeout=4.0)
        r.raise_for_status()
        return [{"name": m["name"], "size": m.get("size", 0)} for m in r.json().get("models", [])]
    except (httpx.HTTPError, KeyError, ValueError):
        return []


@operator_app.get("/setup")
def setup_page(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    return FileResponse(_STATIC / "wizard.html")


@operator_app.get("/api/setup/detect")
def setup_detect(request: Request):
    """Step 1: real GPU / Ollama / Tor checks for the machine."""
    if not _is_local(request):
        return _NOT_FOUND
    import subprocess

    # GPU via nvidia-smi (Ollama can still run on CPU, so absence is a warning, not a failure).
    gpu = {"ok": False, "detail": "no NVIDIA GPU detected (Ollama can still run on CPU)"}
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=8)
        if out.returncode == 0 and out.stdout.strip():
            name, _, mem = out.stdout.strip().splitlines()[0].partition(",")
            gpu = {"ok": True, "detail": f"{name.strip()} · {mem.strip()}"}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    models = _ollama_models()
    ollama = ({"ok": True, "detail": f"running · {len(models)} model(s) available"} if models or
              _ollama_reachable() else {"ok": False, "detail": "not reachable on " +
              os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")})

    # Tor control port reachable (needed only for TRANSPORT=tor, but we always report it).
    cp = int(os.getenv("TOR_CONTROL_PORT", "9051"))
    tor = ({"ok": True, "detail": f"control port {cp} reachable · onion ready"} if _tor_reachable()
           else {"ok": False, "detail": f"control port {cp} not reachable"})

    return {"gpu": gpu, "ollama": ollama, "tor": tor, "models": models}


def _ollama_reachable() -> bool:
    import httpx
    try:
        httpx.get(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags", timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


@operator_app.get("/api/setup/models")
def setup_models(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    return {"models": _ollama_models()}


@operator_app.post("/api/setup/pull")
async def setup_pull(request: Request):
    """Pull a model into Ollama, streaming its progress lines straight through to the wizard."""
    if not _is_local(request):
        return _NOT_FOUND
    import httpx
    name = (await request.json()).get("name", "").strip()
    if not name:
        return JSONResponse({"error": "model name required"}, status_code=400)
    url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/pull"

    def proxy():
        with httpx.stream("POST", url, json={"name": name, "stream": True}, timeout=None) as r:
            for line in r.iter_lines():
                if line:
                    yield line + "\n"

    return StreamingResponse(proxy(), media_type="application/x-ndjson")


@operator_app.post("/api/setup/model")
async def setup_model(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    try:
        updates = config_writer.model_env((await request.json()).get("name", ""))
        config_writer.update_env_file(updates)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **updates}


@operator_app.post("/api/setup/pricing")
async def setup_pricing(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    body = await request.json()
    try:
        updates = config_writer.pricing_env(body.get("price_sat"), body.get("chunk"), body.get("expiry"))
        config_writer.update_env_file(updates)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **updates}


@operator_app.post("/api/setup/payout")
async def setup_payout(request: Request):
    """Step 4 — configure a payout tier. phoenixd provisions a node + returns the seed for the
    back-up step; lnd just writes the manually-entered creds; nwc capability-checks the wallet
    (must support make_invoice to receive) before writing. None touch the live host's creds."""
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    body = await request.json()
    tier = (body.get("tier") or "").lower()
    fields = body.get("fields") or {}
    try:
        updates = config_writer.payout_env(tier, fields)  # validates field presence/format
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if tier == "nwc":
        # Capability guard: a pay-only wallet can't host. Block (not warn) with a clear message.
        from host import payments as pay_mod
        ok, detail = pay_mod.nwc_capability(updates["NWC_URI"])
        if not ok:
            return JSONResponse({"error": detail, "capable": False}, status_code=400)

    if tier == "phoenixd":
        from host import phoenixd_setup
        mode = (body.get("mode") or "generate").lower()
        if mode == "import":
            # Restore an existing wallet from the operator's recovery phrase (local operator port
            # only — already gated; never the onion). Validate BIP39 before touching seed.dat; never
            # log the phrase. The operator already has the seed, so NO backup ceremony is returned.
            from shared import bip39
            ok, reason = bip39.validate_mnemonic(body.get("seed") or "")
            if not ok:
                return JSONResponse({"error": reason}, status_code=400)
            words = bip39.normalize(body.get("seed") or "")
            replace = bool(body.get("replace"))
            # An existing wallet (often auto-generated by a prior 'create new' attempt or regenerated
            # by the durable phoenixd.service) must be handled deliberately: refuse to clobber a
            # FUNDED wallet; for a never-funded one, ask the wizard to confirm replace.
            if phoenixd_setup.is_provisioned() and not replace:
                funded, detail = phoenixd_setup.wallet_funded_status()
                if funded:
                    return JSONResponse(
                        {"error": f"a funded phoenixd wallet already exists ({detail}). Withdraw and "
                                  f"close it first — import won't replace a wallet with funds.",
                         "funded": True}, status_code=409)
                return JSONResponse(
                    {"needs_replace": True, "funded": False, "detail": detail,
                     "message": "An existing (empty) wallet was found. Replacing it archives the old "
                                "~/.phoenix aside and restores your imported seed."}, status_code=409)
            try:
                config_writer.update_env_file(updates)
                result = phoenixd_setup.import_seed(words, replace=replace)
            except Exception as e:  # noqa: BLE001 — surface import failure to the wizard
                return JSONResponse({"error": str(e)[:300]}, status_code=500)
            return {"ok": True, "tier": tier, "imported": True, "service": result["service"]}
        try:
            config_writer.update_env_file(updates)
            result = phoenixd_setup.provision()  # downloads + first-runs phoenixd; writes the password
        except Exception as e:  # noqa: BLE001 — surface provisioning failure to the wizard
            return JSONResponse({"error": str(e)[:300]}, status_code=500)
        # seed_words is shown once in the local wizard, then discarded — never persisted/transmitted.
        return {"ok": True, "tier": tier, "seed_words": result["seed_words"], "service": result["service"]}

    # lnd / nwc: write the env; no provisioning, no seed step. Service restart happens at go-live.
    config_writer.update_env_file(updates)
    return {"ok": True, "tier": tier}


def _tor_reachable() -> bool:
    """Is Tor's control port up (so we can provision a .onion)?"""
    import socket
    cp = int(os.getenv("TOR_CONTROL_PORT", "9051"))
    try:
        with socket.create_connection(("127.0.0.1", cp), timeout=3):
            return True
    except OSError:
        return False


@operator_app.post("/api/setup/golive")
def setup_golive(request: Request):
    """Final step. Pick the transport (Tor when reachable, else a deliberate clearnet fallback),
    INSTALL + enable the sail-host systemd unit (so a host that looks live survives reboot — it was
    never installed before), verify the phoenixd payment service is up when that's the rail, then
    restart sail-host to apply. The wizard then polls /api/status and only shows 'live' once the
    daemon AND the payment API both respond. Anything needing root that we can't do passwordless is
    surfaced as exact commands."""
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer, service_setup

    transport_mode = "tor" if _tor_reachable() else "clearnet"
    config_writer.update_env_file({"TRANSPORT": transport_mode})

    # Durability: install + enable the sail-host unit from the shipped template (was missing).
    sail_host = service_setup.install_sail_host_service()

    # Payment service must actually be running before we let the host be advertised as payable.
    tier = os.getenv("PAYMENTS", "mock").lower()
    payments = {"tier": tier}
    if tier == "phoenixd":
        from host import phoenixd_setup
        # phoenixd.service is installed during the payout step; make sure it's started, then ping it.
        payments["service"] = config_writer.service_command("start", service="phoenixd")
        ok, detail = _ln.ping()
        payments.update(ready=ok, detail=detail)

    restart = config_writer.restart_service()  # apply config / (re)start the host service

    commands = []
    if sail_host.get("install_required"):
        commands += sail_host.get("commands", [])
    if restart.get("restart_required"):
        commands.append(restart["command"])
    if tier == "phoenixd" and payments.get("service", {}).get("ok") is False:
        commands.append(payments["service"]["command"])
    return {"transport": transport_mode, "sail_host": sail_host, "payments": payments,
            "restart": restart, "commands": commands}


# --- host controls (LOCAL ONLY) ---------------------------------------------
# Pause/Resume/Stop&remove the sail-host systemd unit. Same .onion gate as the dashboard. All reuse
# config_writer.service_command (try `sudo -n`, else surface the command) — no new mechanism. None
# of these touch ~/.phoenix, the onion key, or the nsec; remove only affects the systemd unit.
@operator_app.post("/api/control/pause")
def control_pause(request: Request):
    """Stop the service: listing goes stale, daemon stops. Resume brings the same identity back."""
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    return config_writer.service_command("stop")


@operator_app.post("/api/control/resume")
def control_resume(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    return config_writer.service_command("start")


@operator_app.post("/api/control/remove")
async def control_remove(request: Request):
    """Disable + stop the unit now; the unit-FILE deletion still needs a manual sudo step (we never
    auto-rm a system unit), so those commands are surfaced. Gated by type-to-confirm. Funds/keys in
    ~/.phoenix are NOT touched."""
    if not _is_local(request):
        return _NOT_FOUND
    if (await request.json()).get("confirm") != "remove":
        return JSONResponse({"error": "type-to-confirm required"}, status_code=400)
    from host import config_writer
    svc = config_writer.SERVICE
    disable = config_writer.service_command("disable", flags=("--now",))
    return {"disable": disable,
            "manual_commands": [f"sudo rm /etc/systemd/system/{svc}.service",
                                "sudo systemctl daemon-reload"]}
