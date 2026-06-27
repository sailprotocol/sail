"""
sail-host systemd service install (infra punch-list P1).

The wizard's go-live used to only *restart* sail-host — but on a fresh host the unit was never
installed, so a host that looked live died on terminal-close / reboot. This renders the shipped
deploy/sail-host.service.example from the running environment and installs + enables it, mirroring
host/phoenixd_setup.py's install flow: attempt a passwordless install, else surface the exact
commands for the operator to run.
"""
from __future__ import annotations

import getpass
import os
import pathlib
import subprocess
import sys


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _deploy_dir() -> pathlib.Path:
    return repo_root() / "deploy"


def template_path() -> pathlib.Path:
    return _deploy_dir() / "sail-host.service.example"


def render_unit(user: str | None = None, workdir: str | None = None, uvicorn: str | None = None,
                env_file: str | None = None, port: str | None = None) -> str:
    """Fill the shipped template from the running environment (so the installed unit matches how
    this daemon is actually being run)."""
    user = user or os.getenv("USER") or getpass.getuser()
    workdir = workdir or str(repo_root())
    # the venv's uvicorn lives next to the python running us
    uvicorn = uvicorn or str(pathlib.Path(sys.executable).with_name("uvicorn"))
    env_file = env_file or os.getenv("ENV_FILE", ".env.host")
    port = str(port or os.getenv("PORT", "8001"))
    return (template_path().read_text()
            .replace("{{USER}}", user).replace("{{WORKDIR}}", workdir)
            .replace("{{UVICORN}}", uvicorn).replace("{{ENV_FILE}}", env_file)
            .replace("{{PORT}}", port))


def install_sail_host_service(service: str = "sail-host") -> dict:
    """Render the unit, write the operator copy (deploy/sail-host.service, gitignored), then try a
    passwordless install + enable --now. Returns {installed: True} or {installed: False,
    install_required: True, commands: [...], unit, error} so go-live can surface the steps."""
    service = os.getenv("SAIL_SERVICE", service)
    _deploy_dir().mkdir(parents=True, exist_ok=True)
    unit = _deploy_dir() / f"{service}.service"
    unit.write_text(render_unit())
    commands = [
        f"sudo cp {unit} /etc/systemd/system/{service}.service",
        "sudo systemctl daemon-reload",
        f"sudo systemctl enable --now {service}",
    ]
    try:
        for c in commands:
            proc = subprocess.run(["sudo", "-n", *c.split()[1:]],
                                  capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return {"installed": False, "install_required": True, "commands": commands,
                        "unit": str(unit), "error": (proc.stderr or proc.stdout).strip()[:200]}
        return {"installed": True, "unit": str(unit)}
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"installed": False, "install_required": True, "commands": commands,
                "unit": str(unit), "error": str(e)}
