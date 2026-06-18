"""
Client-side session history.

Persists each completed inference locally (gitignored, path via HISTORY_PATH) so the GUI can
list and reopen past sessions. First-party data; lives in the client layer, not the shared core
(the CLI doesn't write history).
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import time


def _path() -> pathlib.Path:
    return pathlib.Path(os.getenv("HISTORY_PATH", "./client_history.json"))


def _load_all() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_all(sessions: list[dict]) -> None:
    p = _path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(sessions, indent=2))
    tmp.replace(p)  # atomic


def save(*, host_pubkey: str, model: str, prompt: str, response: str,
         spent_msat: int, latency_ms: float) -> str:
    """Append a completed session and return its id."""
    sid = secrets.token_hex(6)
    entry = {
        "id": sid,
        "created_at": int(time.time()),
        "host_pubkey": host_pubkey,
        "model": model,
        "prompt": prompt,
        "response": response,
        "spent_msat": spent_msat,
        "latency_ms": latency_ms,
    }
    sessions = _load_all()
    sessions.append(entry)
    _write_all(sessions)
    return sid


def list_summaries() -> list[dict]:
    """Newest-first summaries (omits the full response to keep the list light)."""
    out = []
    for s in reversed(_load_all()):
        out.append({k: s.get(k) for k in
                    ("id", "created_at", "host_pubkey", "model", "prompt", "spent_msat", "latency_ms")})
    return out


def get(sid: str) -> dict | None:
    return next((s for s in _load_all() if s.get("id") == sid), None)


def delete(sid: str) -> bool:
    sessions = _load_all()
    kept = [s for s in sessions if s.get("id") != sid]
    if len(kept) == len(sessions):
        return False
    _write_all(kept)
    return True


def clear() -> int:
    n = len(_load_all())
    _write_all([])
    return n
