"""
Local web client (GUI v1).

A small FastAPI backend that wraps the shared client core (`client/core.py`) and serves a
browser UI on localhost, so non-CLI users can discover hosts, pick one, prompt, watch tokens
stream, and see sats spent. v1 is a local web app; Tauri/desktop packaging is deferred.

    ENV_FILE=.env.client PYTHONPATH=. .venv/bin/uvicorn client.webapp:app --port 8080
    # then open http://127.0.0.1:8080
"""
from __future__ import annotations

import json
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from shared.config import load_env

load_env()  # same dotenv flow as the host daemon; reuses .env.client

from client import core
from client import reputation

app = FastAPI(title="inference-net client")
_STATIC = pathlib.Path(__file__).parent / "static"
_host_cache: dict = {}  # pubkey -> HostListing, populated by /api/hosts for /api/infer lookup


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/api/hosts")
def api_hosts():
    hosts = core.discover_hosts()
    rep = reputation.load()
    _host_cache.clear()
    out = []
    for h in hosts:
        _host_cache[h.pubkey] = h
        m = h.models[0]
        s = rep.get(h.pubkey)
        rep_view = None
        if s:
            attempts = s.get("attempts", 0)
            rep_view = {
                "attempts": attempts,
                "successes": s.get("successes", 0),
                "success_rate": round(s["successes"] / attempts, 2) if attempts else None,
                "ewma_latency_ms": s.get("ewma_latency_ms"),
            }
        out.append({
            "pubkey": h.pubkey,
            "endpoint": h.endpoint,
            "model": {"name": m.name, "price_msat_per_token": m.price_msat_per_token,
                      "context_window": m.context_window},
            "reputation": rep_view,
            "bond_txid": h.bond_txid,  # advisory only
        })
    return {"hosts": out}


@app.post("/api/infer")
async def api_infer(request: Request):
    body = await request.json()
    pubkey = body.get("host_pubkey")
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 64))

    host = _host_cache.get(pubkey)
    if host is None:  # cache miss (e.g. server restarted) -> re-discover
        host = next((h for h in core.discover_hosts() if h.pubkey == pubkey), None)
    if host is None:
        return JSONResponse({"error": "unknown host_pubkey"}, status_code=404)

    def gen():
        # NDJSON: one core event per line. Sync generator -> FastAPI runs it in a threadpool,
        # so the blocking httpx/payment calls don't block the event loop.
        for ev in core.run_inference(host, prompt, max_tokens=max_tokens):
            yield json.dumps(ev) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
