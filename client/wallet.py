"""
Local NWC (Nostr Wallet Connect) wallet store.

Holds the user's NWC connection string (NIP-47) locally so the client can pay host invoices
from the user's OWN wallet — non-custodial, the app never holds funds. The connection string is
a secret (it can spend up to the wallet's budget), so it's stored gitignored like the nsec /
macaroon and never returned to the browser.
"""
from __future__ import annotations

import json
import os
import pathlib


def _path() -> pathlib.Path:
    return pathlib.Path(os.getenv("NWC_PATH", "./client_nwc.json"))


def _parse(uri: str):
    from nostr_sdk import NostrWalletConnectUri
    return NostrWalletConnectUri.parse(uri)  # raises on malformed input


def save(uri: str) -> None:
    """Validate and persist an NWC connection string. Raises if malformed."""
    uri = uri.strip()
    _parse(uri)  # validate format before writing
    p = _path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"uri": uri}))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)  # atomic


def _env_uri() -> str | None:
    env = os.getenv("NWC_URI")
    return env.strip() if env and env.strip() else None


def _store_uri() -> str | None:
    p = _path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("uri") or None
    except Exception:
        return None


def load_uri() -> str | None:
    """The connection string from env NWC_URI (preferred) or the local GUI store; None if neither."""
    return _env_uri() or _store_uri()


def clear() -> bool:
    """Remove the GUI-managed (stored) connection. Returns False if there was no store file. Note an
    env NWC_URI is config, NOT GUI-managed — it can't be cleared here (status reports source='env')."""
    p = _path()
    if p.exists():
        p.unlink()
        return True
    return False


def status() -> dict:
    """Connection status WITHOUT exposing the secret — connected flag, wallet pubkey, and SOURCE
    (env vs store) so the GUI knows whether Disconnect can clear it."""
    env, store = _env_uri(), _store_uri()
    uri = env or store
    source = "env" if env else ("store" if store else None)
    if not uri:
        return {"connected": False, "wallet_pubkey": None, "source": None}
    try:
        # The wallet service pubkey is the URI authority (before '?'); safe to surface.
        pubkey = uri.split("://", 1)[1].split("?", 1)[0]
    except Exception:
        pubkey = None
    return {"connected": True, "wallet_pubkey": pubkey, "source": source}
