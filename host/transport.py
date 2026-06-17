"""
Host transport.

TRANSPORT=clearnet (default) advertises the clearnet/LAN HOST_ENDPOINT as today.
TRANSPORT=tor exposes the daemon as a Tor v3 onion service so it is reachable from any
network (NAT/CGNAT-proof) with the host IP hidden, and advertises the .onion as its endpoint.

This talks to a running Tor over its control port via `stem` (imported lazily here, so the
clearnet path never needs stem or Tor). The onion private key is persisted to ONION_KEY_PATH
so the .onion address is stable across restarts. The key is a secret — keep it gitignored.
"""
from __future__ import annotations

import os
import pathlib

# Holds the Tor control connection for the process lifetime. A non-detached ephemeral onion
# service is torn down when its controlling connection closes, so we must keep this alive.
_controller = None


def setup_onion(port: int) -> str:
    """Create (or restore) a v3 onion service mapping onion:80 -> 127.0.0.1:port.

    Returns the endpoint URL, e.g. "http://<56-char-addr>.onion".
    """
    global _controller
    from stem.control import Controller  # lazy: only needed for TRANSPORT=tor

    control_port = int(os.getenv("TOR_CONTROL_PORT", "9051"))
    key_path = pathlib.Path(os.getenv("ONION_KEY_PATH", "./onion.key"))

    _controller = Controller.from_port(port=control_port)
    _controller.authenticate()  # cookie auth (user must be able to read Tor's auth cookie)

    if key_path.exists():
        key_type, _, key_content = key_path.read_text().strip().partition(":")
    else:
        key_type, key_content = "NEW", "ED25519-V3"

    resp = _controller.create_ephemeral_hidden_service(
        {80: port},
        key_type=key_type,
        key_content=key_content,
        await_publication=True,
        detached=False,
    )

    # On first creation Tor returns the freshly generated key; persist it for a stable address.
    if resp.private_key:
        key_path.write_text(f"{resp.private_key_type}:{resp.private_key}")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass

    return f"http://{resp.service_id}.onion"
