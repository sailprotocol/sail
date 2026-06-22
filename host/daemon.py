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
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

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
            _record_settled(sid[:4], s["emitted"], spent)
            _sessions.pop(sid, None)
            yield done_trailer(spent)

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
    body = await request.json()
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 64)) or 64
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


@app.get("/")
def dashboard(request: Request):
    if not _is_local(request):
        return JSONResponse({"error": "not found"}, status_code=404)  # don't expose over the onion
    return FileResponse(_STATIC / "dashboard.html")


@app.get("/api/status")
def api_status(request: Request):
    if not _is_local(request):
        return JSONResponse({"error": "not found"}, status_code=404)
    t = _today()
    # in-progress metered generations (started, not yet done) shown as "streaming"
    streaming = [{"tag": sid[:4], "model": _model.name, "tokens": v["emitted"],
                  "msat": v["charged_msat"], "state": "streaming"}
                 for sid, v in _sessions.items() if v.get("gen") is not None]
    return {
        "state": "live",
        "pubkey": PUBKEY,
        "onion": ENDPOINT,
        "transport": TRANSPORT,
        "model": _model.name,
        "payments": os.getenv("PAYMENTS", "mock").lower(),
        "price_msat_per_token": PRICE_MSAT_PER_TOKEN,
        "chunk_tokens": CHUNK_TOKENS,
        "invoice_expiry_s": BOLT11_EXPIRY_SECONDS,
        "uptime_s": int(time.time() - _START_TIME),
        "streaming_now": len(streaming),
        "tokens_today": t["tokens"],
        "sats_today": t["msat"] // 1000,
        "activity": streaming + list(_activity),
    }


@app.get("/api/selftest")
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


@app.get("/setup")
def setup_page(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    return FileResponse(_STATIC / "wizard.html")


@app.get("/api/setup/detect")
def setup_detect(request: Request):
    """Step 1: real GPU / Ollama / Tor checks for the machine."""
    if not _is_local(request):
        return _NOT_FOUND
    import socket
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
    tor = {"ok": False, "detail": f"control port {cp} not reachable"}
    try:
        with socket.create_connection(("127.0.0.1", cp), timeout=3):
            tor = {"ok": True, "detail": f"control port {cp} reachable · onion ready"}
    except OSError:
        pass

    return {"gpu": gpu, "ollama": ollama, "tor": tor, "models": models}


def _ollama_reachable() -> bool:
    import httpx
    try:
        httpx.get(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags", timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


@app.get("/api/setup/models")
def setup_models(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    return {"models": _ollama_models()}


@app.post("/api/setup/pull")
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


@app.post("/api/setup/model")
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


@app.post("/api/setup/pricing")
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


@app.post("/api/setup/payout")
async def setup_payout(request: Request):
    """Step 4. Only phoenixd is wired in setup: it provisions a node and returns the seed for the
    wizard's back-up step. lnd/nwc tiers are not enabled here yet (one seam at a time)."""
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    body = await request.json()
    tier = (body.get("tier") or "").lower()
    if tier != "phoenixd":
        return JSONResponse({"error": f"payout tier {tier!r} is not available in setup yet"},
                            status_code=400)
    try:
        config_writer.update_env_file(config_writer.payout_env(tier, body.get("fields") or {}))
        from host import phoenixd_setup
        result = phoenixd_setup.provision()  # downloads + first-runs phoenixd; writes the password
    except Exception as e:  # noqa: BLE001 — surface provisioning failure to the wizard
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    # seed_words is shown once in the local wizard, then discarded; never persisted/transmitted here.
    return {"ok": True, "tier": tier, "seed_words": result["seed_words"], "service": result["service"]}


@app.post("/api/setup/golive")
def setup_golive(request: Request):
    """Final step: restart sail-host so it loads the new config, creates the onion, and publishes
    the listing. Restart may kill this very process — the wizard then polls /api/status until live."""
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    return config_writer.restart_service()


# --- host controls (LOCAL ONLY) ---------------------------------------------
# Pause/Resume/Stop&remove the sail-host systemd unit. Same .onion gate as the dashboard. All reuse
# config_writer.service_command (try `sudo -n`, else surface the command) — no new mechanism. None
# of these touch ~/.phoenix, the onion key, or the nsec; remove only affects the systemd unit.
@app.post("/api/control/pause")
def control_pause(request: Request):
    """Stop the service: listing goes stale, daemon stops. Resume brings the same identity back."""
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    return config_writer.service_command("stop")


@app.post("/api/control/resume")
def control_resume(request: Request):
    if not _is_local(request):
        return _NOT_FOUND
    from host import config_writer
    return config_writer.service_command("start")


@app.post("/api/control/remove")
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
