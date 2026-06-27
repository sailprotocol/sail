"""
Client-side reputation.

The client records its OWN experience with each host (success/failure, latency, signature
validity) to a local gitignored file and ranks discovered hosts by it. Because this is
first-party data a host cannot inflate it — it only reflects how that host treated THIS client.

This is deliberately NOT global/public reputation: signed receipts, on-chain bonds, and slashing
are sybil-gameable without bonded arbitration and are deferred to Phase 4. A listing's bond_txid
stays an advisory display hint only, never trusted here.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

REP_MIN_ATTEMPTS = 3    # require this many tries before judging a host harshly
REP_FAIL_RATE = 0.34    # success rate below this (after MIN_ATTEMPTS) -> drop the host
REP_MAX_CONSEC = 3      # this many failures in a row -> drop the host
_EWMA = 0.3             # latency smoothing factor


def _path() -> pathlib.Path:
    return pathlib.Path(os.getenv("REPUTATION_PATH", "./client_reputation.json"))


def load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def record(pubkey: str, *, success: bool, latency_ms: float | None = None,
           sig_valid: bool = True) -> None:
    """Update this client's experience with `pubkey` and persist it atomically."""
    rep = load()
    s = rep.get(pubkey) or {"attempts": 0, "successes": 0, "failures": 0,
                            "consecutive_failures": 0, "last_latency_ms": None,
                            "ewma_latency_ms": None, "sig_valid": True, "last_seen": 0}
    s["attempts"] += 1
    s["sig_valid"] = bool(sig_valid)
    s["last_seen"] = int(time.time())
    if success:
        s["successes"] += 1
        s["consecutive_failures"] = 0
        if latency_ms is not None:
            s["last_latency_ms"] = round(latency_ms, 1)
            prev = s["ewma_latency_ms"]
            s["ewma_latency_ms"] = round(
                latency_ms if prev is None else _EWMA * latency_ms + (1 - _EWMA) * prev, 1)
    else:
        s["failures"] += 1
        s["consecutive_failures"] += 1
    rep[pubkey] = s

    p = _path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(rep, indent=2))
    tmp.replace(p)  # atomic


def _success_rate(s: dict) -> float:
    return s["successes"] / s["attempts"] if s["attempts"] else 0.5


def _is_bad(s: dict | None) -> bool:
    """A host we've tried enough and that keeps failing — drop it from candidates."""
    if not s:
        return False
    if s.get("consecutive_failures", 0) >= REP_MAX_CONSEC:
        return True
    return s["attempts"] >= REP_MIN_ATTEMPTS and _success_rate(s) < REP_FAIL_RATE


def partition(hosts, rep: dict):
    """Split discovered hosts into (kept_best_first, hidden). Kept are ranked by the client's own
    experience; hidden are repeated-failers dropped from candidates. Unknown hosts get a neutral
    score so they're still tried, but below proven-good ones."""
    kept = [h for h in hosts if not _is_bad(rep.get(h.pubkey))]
    hidden = [h for h in hosts if _is_bad(rep.get(h.pubkey))]

    def key(h):
        s = rep.get(h.pubkey)
        if not s:
            return (0.5, 0.0)               # unknown: neutral, tried but below proven-good
        return (_success_rate(s), -(s.get("ewma_latency_ms") or 0.0))  # rate then low latency

    return sorted(kept, key=key, reverse=True), hidden


def rank(hosts, rep: dict):
    """Discovered hosts best-first by local experience; repeated failers dropped."""
    return partition(hosts, rep)[0]


def reset() -> bool:
    """Wipe the local reputation store. Returns True if a file was removed."""
    p = _path()
    if p.exists():
        p.unlink()
        return True
    return False
