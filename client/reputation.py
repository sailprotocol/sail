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

REP_HIDE_CONSEC = 3     # hide only after THIS many consecutive HOST failures (never on the first)
_EWMA = 0.3             # latency smoothing factor


def _cooldown() -> int:
    """Seconds a hidden host stays hidden before it re-surfaces on its own (no manual reset). The
    penalty decays so a transient blip (flaky Tor, momentary host hiccup) clears itself."""
    return int(os.getenv("REP_COOLDOWN_SECONDS", "600"))


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
        # Only HOST-fault outcomes reach here — run_inference does NOT record client-side payment
        # failures (dead/unreachable wallet, no route, NWC relay down), so they never bury a host.
        s["failures"] += 1
        s["consecutive_failures"] += 1
        s["last_fail_ts"] = int(time.time())
    rep[pubkey] = s

    p = _path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(rep, indent=2))
    tmp.replace(p)  # atomic


def _success_rate(s: dict) -> float:
    return s["successes"] / s["attempts"] if s["attempts"] else 0.5


def _is_bad(s: dict | None) -> bool:
    """Hide a host ONLY when it has racked up REP_HIDE_CONSEC consecutive host failures AND the last
    one was within the cooldown. A single failure never hides it; after the cooldown the host
    re-surfaces on its own (no manual reset). Poor success-rate still down-RANKS (see key()) but
    never hides — so a good host isn't buried by a transient blip, while a genuinely dead host
    keeps re-failing and stays effectively de-prioritized."""
    if not s:
        return False
    if s.get("consecutive_failures", 0) < REP_HIDE_CONSEC:
        return False
    return (time.time() - s.get("last_fail_ts", 0)) < _cooldown()


def hidden_reason(s: dict | None) -> dict:
    """Why a host is hidden + when it clears, for --list. {consecutive, age_s, clears_in_s}."""
    if not s:
        return {}
    age = int(time.time() - s.get("last_fail_ts", 0))
    return {"consecutive": s.get("consecutive_failures", 0), "age_s": age,
            "clears_in_s": max(0, _cooldown() - age)}


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
