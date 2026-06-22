"""
Config-writer (build spec §7).

The setup wizard collects what `.env.host` needs and POSTs it here; this module validates,
writes the gitignored `.env.host` atomically (preserving comments + order), and (re)starts the
`sail-host` systemd service so the new config takes effect.

It NEVER logs secret values (phoenixd password, NWC string, macaroon paths are fine). Writes are
atomic (temp file + rename) and the file is chmod 600 because it holds secrets.

Restart: the daemon runs as the operator (not root) and `sail-host` is a *system* unit, so it
tries `sudo -n systemctl restart sail-host` and, if that is not permitted, returns the command for
the operator to run. Install `deploy/sail-sudoers` to get the one-click path.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile

SERVICE = "sail-host"


def env_host_path() -> pathlib.Path:
    """The host env file this process is configured to load (ENV_FILE, default .env.host)."""
    return pathlib.Path(os.getenv("ENV_FILE", ".env.host"))


# --- atomic, comment-preserving .env writer ---------------------------------
_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _format_value(value: str) -> str:
    """Quote a value only when it would otherwise be ambiguous to python-dotenv (spaces, #, or
    leading/trailing whitespace). Our values are paths / connection strings / passwords — single
    line, no newlines (rejected by the caller)."""
    if value == "" or re.search(r"[\s#'\"]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env_file(updates: dict[str, str], path: pathlib.Path | None = None) -> pathlib.Path:
    """Set each KEY=value in `path`, replacing an existing assignment in place (keeping comments,
    blank lines, and ordering) or appending if absent. Atomic; result is chmod 600."""
    path = path or env_host_path()
    for k, v in updates.items():
        if not _KEY_RE.match(f"{k}="):
            raise ValueError(f"invalid env key: {k!r}")
        if "\n" in v or "\r" in v:
            raise ValueError(f"env value for {k} must be single-line")

    existing = path.read_text().splitlines(keepends=True) if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in existing:
        m = _KEY_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"{key}={_format_value(remaining.pop(key))}{nl}")
        else:
            out.append(line)
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        for key, val in remaining.items():
            out.append(f"{key}={_format_value(val)}\n")

    # Atomic replace within the same dir so the rename can't cross filesystems.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent or "."), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(out)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


# --- per-tier / per-step env contracts (validation) -------------------------
def payout_env(tier: str, fields: dict[str, str]) -> dict[str, str]:
    """Build the `.env.host` updates for a payout tier (spec §7). Only the phoenixd tier is wired
    in setup right now; lnd/nwc are deliberately not enabled yet (one seam at a time)."""
    tier = (tier or "").lower()
    if tier == "phoenixd":
        # The password is written by provisioning (it owns the seed/node); the GUI's only input is
        # the seed-backup confirmation. We just select the rail + ensure the API URL has a default.
        url = (fields or {}).get("api_url", "").strip() or "http://127.0.0.1:9740"
        return {"PAYMENTS": "phoenixd", "PHOENIXD_API_URL": url}
    if tier in ("lnd", "nwc"):
        raise ValueError(f"payout tier {tier!r} is not available in setup yet")
    raise ValueError(f"unknown payout tier: {tier!r}")


def pricing_env(price_sat_per_token: float, chunk_tokens: int, expiry_seconds: int) -> dict[str, str]:
    """Validate the pricing step and map sat/token (GUI) -> msat/token (env). 1-sat floor matches
    the invoice mint floor so sub-sat pricing can't request an un-mintable invoice."""
    try:
        msat = round(float(price_sat_per_token) * 1000)
        chunk = int(chunk_tokens)
        expiry = int(expiry_seconds)
    except (TypeError, ValueError):
        raise ValueError("price, chunk, and expiry must be numbers")
    if msat < 1000:
        raise ValueError("price must be at least 1 sat/token")
    if chunk < 1:
        raise ValueError("chunk size must be at least 1 token")
    if expiry < 30:
        raise ValueError("invoice expiry must be at least 30 seconds")
    return {"PRICE_MSAT_PER_TOKEN": str(msat), "CHUNK_TOKENS": str(chunk),
            "BOLT11_EXPIRY_SECONDS": str(expiry)}


def model_env(name: str) -> dict[str, str]:
    """Select an Ollama model to serve."""
    name = (name or "").strip()
    if not name or re.search(r"\s", name):
        raise ValueError("model name must be a non-empty single token")
    return {"MODEL": "ollama", "OLLAMA_MODEL": name}


# --- service restart --------------------------------------------------------
def restart_service(service: str = SERVICE) -> dict:
    """Try a passwordless restart; if sudo isn't permitted (no deploy/sail-sudoers installed),
    report the command for the operator to run instead. Returns a JSON-able dict either way."""
    cmd = ["sudo", "-n", "systemctl", "restart", service]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"restarted": False, "restart_required": True,
                "command": f"sudo systemctl restart {service}", "error": str(e)}
    if proc.returncode == 0:
        return {"restarted": True}
    return {"restarted": False, "restart_required": True,
            "command": f"sudo systemctl restart {service}",
            "error": (proc.stderr or proc.stdout).strip()[:200]}
