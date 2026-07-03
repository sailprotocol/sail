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


class OnionCollisionError(RuntimeError):
    """Tor already has this host's onion registered (another instance holds it, or a prior run
    didn't release it). The message is operator-facing with a recovery step."""


class TorControlError(RuntimeError):
    """Tor's control port couldn't be used — the auth cookie is unreadable (user not in the
    'debian-tor' group in this session), or Tor isn't running with ControlPort enabled. The message
    is operator-facing with a concrete recovery step, so the daemon exits clean instead of dumping a
    raw stem traceback."""


def setup_onion(port: int) -> str:
    """Create (or restore) a v3 onion service mapping onion:80 -> 127.0.0.1:port.

    Returns the endpoint URL, e.g. "http://<56-char-addr>.onion".
    """
    global _controller
    from stem.control import Controller  # lazy: only needed for TRANSPORT=tor

    control_port = int(os.getenv("TOR_CONTROL_PORT", "9051"))
    key_path = pathlib.Path(os.getenv("ONION_KEY_PATH", "./onion.key"))

    try:
        _controller = Controller.from_port(port=control_port)
        _controller.authenticate()  # cookie auth (user must be able to read Tor's auth cookie)
    except Exception as e:  # noqa: BLE001 — translate raw stem/OS errors into an actionable message
        low = str(e).lower()
        if isinstance(e, PermissionError) or "cookie" in low:
            # The single biggest onboarding footgun: user added to 'debian-tor' but the group isn't
            # active in this session, so Tor's control-auth cookie is unreadable.
            raise TorControlError(
                "can't read Tor's control-auth cookie — your user isn't in the 'debian-tor' group in "
                "THIS session. Check with `groups | grep debian-tor`: if it's empty, a plain log-out/in "
                "or a fresh SSH session often does NOT refresh groups — fully REBOOT (or run "
                "`exec su - $USER`), then start again. Running as the sail-host systemd service avoids "
                "this entirely (it starts with the right group from boot)."
            ) from e
        if isinstance(e, (ConnectionError, OSError)) or "refused" in low or "unable to connect" in low:
            raise TorControlError(
                f"can't reach Tor's control port at 127.0.0.1:{control_port} — is Tor running with "
                f"ControlPort enabled? Verify `sudo ss -ltnp | grep {control_port}` shows a LISTENer "
                f"(see the run-a-host guide's Tor setup)."
            ) from e
        raise

    if key_path.exists():
        key_type, _, key_content = key_path.read_text().strip().partition(":")
    else:
        key_type, key_content = "NEW", "ED25519-V3"

    try:
        resp = _controller.create_ephemeral_hidden_service(
            {80: port},
            key_type=key_type,
            key_content=key_content,
            await_publication=True,
            detached=False,
        )
    except Exception as e:  # noqa: BLE001 — translate the raw stem error into an actionable one
        # Our onion (derived from ONION_KEY_PATH) is already registered in Tor. With detached=False
        # a clean shutdown releases it, so a collision means another live instance is holding it (or
        # a prior run left it registered). We can't adopt another control connection's service, so
        # surface a clear recovery step rather than a raw stem traceback.
        if "collision" in str(e).lower():
            raise OnionCollisionError(
                "Tor already has this host's onion registered — another SAIL instance is likely "
                "running (it holds the onion), or a previous run didn't release it. Stop the other "
                "instance (`sudo systemctl stop sail-host`, or kill it), or restart Tor "
                "(`sudo systemctl restart tor`), then start again."
            ) from e
        raise

    # On first creation Tor returns the freshly generated key; persist it for a stable address.
    if resp.private_key:
        key_path.write_text(f"{resp.private_key_type}:{resp.private_key}")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass

    return f"http://{resp.service_id}.onion"
