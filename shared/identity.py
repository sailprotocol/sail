"""
Persistent host identity.

A host is identified, aliased, and signs its listings with ONE Nostr secret key. That key must be
STABLE across restarts — minting a fresh key every boot gives a new pubkey + alias each time and
destroys any reputation/identity continuity (the host-#2 bring-up bug). Resolution order:

  1. NOSTR_HOST_NSEC env — explicit override / existing deployments (host #1).
  2. a persisted key file — HOST_KEY_PATH (default ~/.config/inference-net/host.nsec).
  3. first run: generate one, persist it 0600, and reuse it forever after.

This is independent of REGISTRY/TRANSPORT mode, so a clearnet/local host gets a real, stable
pubkey too — no throwaway `host_xxxx` placeholders are ever published. The secret never leaves
this machine and is never committed.
"""
from __future__ import annotations

import os
import pathlib


def key_path() -> pathlib.Path:
    return pathlib.Path(
        os.path.expanduser(os.getenv("HOST_KEY_PATH", "~/.config/inference-net/host.nsec"))
    )


def host_keys():
    """The host's nostr_sdk Keys, creating + persisting one on first run."""
    from nostr_sdk import Keys

    env = os.getenv("NOSTR_HOST_NSEC")
    if env and env.strip():
        return Keys.parse(env.strip())

    path = key_path()
    if path.exists():
        return Keys.parse(path.read_text().strip())

    keys = Keys.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(keys.secret_key().to_bech32())
    try:
        path.chmod(0o600)  # secret — owner-only
    except OSError:
        pass
    return keys


def host_pubkey_hex() -> str:
    """The host's stable public key (hex). Identity + alias are derived from this."""
    return host_keys().public_key().to_hex()
