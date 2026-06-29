"""
Relay-publish polish (dry-run findings F11 + F14).

F11 — the host must NOT announce a discoverable listing to PUBLIC relays until it is
live-to-serve (a real payout backend, i.e. past go-live). A fresh host still in the wizard
(PAYMENTS=mock) otherwise leaks a ghost listing onto the public relays. The LOCAL registry
(dev/test) is never withheld.

F14 — a partial publish (one relay accepts, another times out) must read as "published to N of
M", with the stragglers a soft retry note — not a scary REJECTED. Only ZERO relays accepting is
a real failure.

Run standalone (`python tests/test_publish_gate.py`) or under pytest (the daemon-integration
tests need pytest's monkeypatch).
"""
from __future__ import annotations

import os
import pathlib
import sys
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Import the daemon with a hermetic mock/local config (matches the other daemon tests).
os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local")
import host.daemon as d  # noqa: E402


@contextmanager
def _env(**kv):
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- F11: the public-relay gate -------------------------------------------
def test_gate_withholds_public_relays_when_not_live():
    with _env(REGISTRY="nostr", PAYMENTS="mock"):
        reason = d._public_publish_withheld()
    assert reason and "setup" in reason.lower(), reason


def test_gate_allows_public_relays_once_live():
    for backend in ("phoenixd", "lnd", "nwc"):
        with _env(REGISTRY="nostr", PAYMENTS=backend):
            assert d._public_publish_withheld() is None, backend


def test_gate_never_withholds_local_registry():
    # dev/test path publishes to the LOCAL dir even pre-go-live (smoke test relies on this)
    with _env(REGISTRY="local", PAYMENTS="mock"):
        assert d._public_publish_withheld() is None


def test_live_to_serve_signal():
    with _env(PAYMENTS="mock"):
        assert d._live_to_serve() is False
    with _env(PAYMENTS="phoenixd"):
        assert d._live_to_serve() is True


# ---- F11: publish_listing honors the gate (integration) -------------------
def _arm(monkeypatch):
    """Record registry.publish calls; neutralize side effects (onion, heartbeat thread)."""
    calls = []
    monkeypatch.setattr(d.registry, "publish",
                        lambda listing: calls.append(listing) or {"success": ["wss://r"], "failed": {}})
    monkeypatch.setattr(d, "TRANSPORT", "clearnet")        # skip onion setup
    monkeypatch.setattr(d, "REANNOUNCE_SECONDS", 0)        # don't spawn the heartbeat thread
    monkeypatch.setattr(d.moderation, "assert_can_serve", lambda model: None)
    return calls


def test_publish_listing_skips_public_relays_pre_golive(monkeypatch):
    calls = _arm(monkeypatch)
    monkeypatch.setenv("REGISTRY", "nostr")
    monkeypatch.setenv("PAYMENTS", "mock")
    d.publish_listing()
    assert calls == [], "a not-yet-live host must not publish to public relays"


def test_publish_listing_publishes_once_live(monkeypatch):
    calls = _arm(monkeypatch)
    monkeypatch.setenv("REGISTRY", "nostr")
    monkeypatch.setenv("PAYMENTS", "phoenixd")
    d.publish_listing()
    assert len(calls) == 1, "a live host must publish its listing"


def test_publish_listing_local_registry_publishes_pre_golive(monkeypatch):
    calls = _arm(monkeypatch)
    monkeypatch.setenv("REGISTRY", "local")
    monkeypatch.setenv("PAYMENTS", "mock")
    d.publish_listing()
    assert len(calls) == 1, "local dev/test registry must still publish"


# ---- F14: partial vs total publish messaging ------------------------------
def _logcap(result):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        d._log_publish(result, "publish")
    return buf.getvalue()


def test_partial_publish_reads_as_success_not_rejected():
    out = _logcap({"success": ["wss://relay.damus.io"], "failed": {"wss://nos.lol": "timeout"}})
    assert "published to 1 of 2" in out, out
    assert "REJECTED" not in out, out
    assert "WARNING" not in out, out
    assert "retry on the next heartbeat" in out, out


def test_full_success_has_no_failure_note():
    out = _logcap({"success": ["wss://a", "wss://b"], "failed": {}})
    assert "published to 2 of 2" in out, out
    assert "didn't accept" not in out and "WARNING" not in out, out


def test_zero_accepted_is_a_real_failure():
    out = _logcap({"success": [], "failed": {"wss://a": "timeout", "wss://b": "refused"}})
    assert "WARNING" in out and "NO relay accepted" in out, out
    assert "published to" not in out, out


if __name__ == "__main__":
    # Standalone: run the no-monkeypatch tests; the integration ones need pytest.
    simple = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v) and "monkeypatch" not in v.__code__.co_varnames]
    for fn in simple:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[publish-gate] {len(simple)} tests PASS "
          f"(run `pytest tests/test_publish_gate.py` for the publish_listing integration tests)")
