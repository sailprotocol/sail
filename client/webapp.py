"""
Local web client (GUI v1).

A small FastAPI backend that wraps the shared client core (`client/core.py`) and serves a
browser UI on localhost. Adds session history (persisted locally), host-unreachable handling
with auto-failover to the next-ranked host, and markdown rendering (frontend). v1 is a local
web app; Tauri/desktop packaging is deferred.

    ENV_FILE=.env.client PYTHONPATH=. .venv/bin/uvicorn client.webapp:app --port 8080
    # then open http://127.0.0.1:8080
"""
from __future__ import annotations

import json
import pathlib

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from shared.config import load_env

load_env()  # same dotenv flow as the host daemon; reuses .env.client

from client import core
from client import reputation
from client import history

app = FastAPI(title="inference-net client")
_STATIC = pathlib.Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/api/hosts")
def api_hosts():
    hosts = core.discover_hosts()
    rep = reputation.load()
    out = []
    for h in hosts:
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


class InferRequest(BaseModel):
    host_pubkey: str
    prompt: str
    max_tokens: int = 64


def _infer_stream(start_pubkey: str, prompt: str, max_tokens: int):
    """Stream core events as NDJSON, auto-failing-over to the next-ranked host when a host is
    unreachable before any token arrives, and saving a history entry on completion."""
    ranked = core.discover_hosts()  # PoW-filtered, allowlisted, reputation-ranked
    chosen = next((h for h in ranked if h.pubkey == start_pubkey), None)
    order = ([chosen] if chosen else []) + [h for h in ranked if h.pubkey != start_pubkey]
    if not order:
        yield json.dumps({"type": "error", "kind": "unreachable",
                          "message": "no hosts available"}) + "\n"
        return

    for i, host in enumerate(order):
        if i > 0:  # we moved here because an earlier host was unreachable
            yield json.dumps({"type": "failover", "to_pubkey": host.pubkey,
                              "model": host.models[0].name}) + "\n"
        emitted = 0
        buf: list[str] = []
        advanced = False
        for ev in core.run_inference(host, prompt, max_tokens=max_tokens):
            if ev["type"] == "token":
                emitted += 1
                buf.append(ev["text"])
                yield json.dumps(ev) + "\n"
            elif ev["type"] == "done":
                try:
                    history.save(host_pubkey=host.pubkey, model=host.models[0].name,
                                 prompt=prompt, response="".join(buf),
                                 spent_msat=ev["spent_msat"], latency_ms=ev["latency_ms"])
                except Exception:
                    pass  # history is best-effort; never break the response on a write error
                yield json.dumps(ev) + "\n"
                return
            elif ev["type"] == "error":
                # Clean failover only: unreachable, nothing streamed yet, another host remains.
                if ev.get("kind") == "unreachable" and emitted == 0 and i < len(order) - 1:
                    advanced = True
                    break
                yield json.dumps(ev) + "\n"
                return
        if not advanced:
            return
    yield json.dumps({"type": "error", "kind": "unreachable",
                      "message": "all candidate hosts were unreachable"}) + "\n"


@app.post("/api/infer")
def api_infer(req: InferRequest):
    return StreamingResponse(
        _infer_stream(req.host_pubkey, req.prompt, req.max_tokens),
        media_type="application/x-ndjson",
    )


# --- session history --------------------------------------------------------
@app.get("/api/history")
def api_history():
    return {"sessions": history.list_summaries()}


@app.get("/api/history/{sid}")
def api_history_one(sid: str):
    s = history.get(sid)
    return s if s else JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/history/{sid}")
def api_history_delete(sid: str):
    return {"deleted": history.delete(sid)}


@app.delete("/api/history")
def api_history_clear():
    return {"cleared": history.clear()}
