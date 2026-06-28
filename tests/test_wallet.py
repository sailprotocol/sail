"""
Wallet connect/disconnect (first-contact P4 — the Disconnect button was a no-op).

Disconnect must clear the GUI-managed (stored) connection AND drop the cached Nwc client (so a
reconnect can't keep paying from the old wallet). An env NWC_URI is config — it can't be cleared
from the GUI, and status must report source='env' so that's shown, not a silent no-op.

Run standalone (`python tests/test_wallet.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nostr_sdk import Keys  # noqa: E402
from client import core, wallet  # noqa: E402


def _uri() -> str:
    w, s = Keys.generate(), Keys.generate()
    return (f"nostr+walletconnect://{w.public_key().to_hex()}"
            f"?relay=wss://relay.example&secret={s.secret_key().to_hex()}")


@contextmanager
def _env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_status_source_none_store_env():
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "nwc.json")
        with _env(NWC_URI=None, NWC_PATH=store):
            assert wallet.status() == {"connected": False, "wallet_pubkey": None, "source": None}
            wallet.save(_uri())
            s = wallet.status()
            assert s["connected"] is True and s["source"] == "store" and s["wallet_pubkey"]
            assert wallet.clear() is True
            assert wallet.status()["connected"] is False
        with _env(NWC_URI=_uri(), NWC_PATH=store):
            s = wallet.status()
            assert s["connected"] is True and s["source"] == "env"


def test_connect_and_disconnect_reset_cached_client():
    from fastapi.testclient import TestClient
    from client import webapp
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "nwc.json")
        with _env(NWC_URI=None, NWC_PATH=store, PAYMENTS="nwc"):
            c = TestClient(webapp.app)
            core._nwc = object()                      # simulate a live cached wallet client
            r = c.post("/api/wallet", json={"uri": _uri()})
            assert r.status_code == 200 and r.json()["source"] == "store", r.json()
            assert core._nwc is None, "connect must reset the cached Nwc client"

            core._nwc = object()
            r = c.delete("/api/wallet")
            assert r.json()["connected"] is False, r.json()
            assert core._nwc is None, "disconnect must reset the cached Nwc client"
            assert not pathlib.Path(store).exists(), "store file must be removed"


def test_disconnect_with_env_pinned_reports_env_not_silent_noop():
    """DELETE clears the store, but an env NWC_URI still wins — status stays connected with
    source='env' so the GUI shows it's config-pinned (the bug Rob hit was this looking like nothing
    happened)."""
    from fastapi.testclient import TestClient
    from client import webapp
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "nwc.json")
        with _env(NWC_URI=_uri(), NWC_PATH=store, PAYMENTS="nwc"):
            c = TestClient(webapp.app)
            s = c.delete("/api/wallet").json()
            assert s["connected"] is True and s["source"] == "env", s


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[wallet] {len(fns)} tests PASS")
