"""
Go-live unit rendering (dry-run finding F2).

A fresh operator (`sailtest` in /home/sailtest/sail) saw the go-live screen print rob's path
(`sudo cp /home/rob/dev/sail/deploy/sail-host.service ...`) and the unit was never written for
their user. Root cause: the rendered `User=` came from $USER (which carries the invoking user
under sudo/su), and nothing pinned workdir/uvicorn to the running process.

These tests pin the contract: the rendered unit + the displayed install commands must use the
RUNNING process's user/workdir/venv — never a baked-in or $USER-derived value — and the unit
file must be written to disk before the cp command that references it is shown.

Run standalone (`python tests/test_service_setup.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from host import service_setup  # noqa: E402


@contextmanager
def _env(**kv):
    """Temporarily set/clear env vars; restore on exit."""
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


def test_render_fills_every_placeholder_from_explicit_values():
    unit = service_setup.render_unit(
        user="alice", workdir="/home/alice/sail",
        uvicorn="/home/alice/sail/.venv/bin/uvicorn", env_file=".env.host", port="9999")
    assert "User=alice" in unit
    assert "WorkingDirectory=/home/alice/sail" in unit
    assert "ExecStart=/home/alice/sail/.venv/bin/uvicorn host.daemon:app --port 9999" in unit
    assert "Environment=ENV_FILE=.env.host" in unit
    # No unfilled placeholders may survive (the comment line used to leak {{...}} tokens).
    assert "{{" not in unit and "}}" not in unit, "unsubstituted placeholder left in unit"


def test_user_comes_from_process_owner_not_USER_env():
    """Under sudo/su, $USER is the invoking user (e.g. rob). The unit must ignore it and use the
    real process owner — otherwise a recruit installs a service that runs as the wrong account."""
    real = service_setup.current_user()
    with _env(USER="ghost-should-be-ignored"):
        unit = service_setup.render_unit()
    assert f"User={real}" in unit
    assert "ghost-should-be-ignored" not in unit


def test_workdir_and_uvicorn_track_the_running_process():
    unit = service_setup.render_unit()
    assert f"WorkingDirectory={service_setup.repo_root()}" in unit
    # uvicorn must be the venv binary beside the running interpreter, not a system path.
    expect = str(pathlib.Path(sys.executable).with_name("uvicorn"))
    assert f"ExecStart={expect} host.daemon:app" in unit


def test_install_writes_unit_before_showing_cp_and_uses_running_identity(tmp_path, monkeypatch):
    """install_sail_host_service must (1) write the rendered unit to disk and (2) return cp/install
    commands that reference that exact written path — for THIS user, not a baked-in one."""
    deploy = tmp_path / "deploy"
    real_template = service_setup.template_path()  # capture before redirecting the deploy dir
    monkeypatch.setattr(service_setup, "_deploy_dir", lambda: deploy)
    monkeypatch.setattr(service_setup, "template_path", lambda: real_template)

    # Force the passwordless install to "fail" so the test is hermetic (no real sudo/systemctl)
    # and we exercise the surface-the-commands path the recruit actually saw.
    class _Fail:
        returncode = 1
        stderr = "sudo: a password is required"
        stdout = ""

    monkeypatch.setattr(service_setup.subprocess, "run", lambda *a, **k: _Fail())
    monkeypatch.setenv("SAIL_SERVICE", "sail-host-test")

    res = service_setup.install_sail_host_service()

    unit_path = deploy / "sail-host-test.service"
    assert unit_path.exists(), "unit must be written to disk before cp is shown"
    body = unit_path.read_text()
    assert f"User={service_setup.current_user()}" in body
    assert f"WorkingDirectory={service_setup.repo_root()}" in body

    assert res["install_required"] is True
    # The displayed cp command references the file we actually wrote (no /home/rob/... baked path).
    cp = next(c for c in res["commands"] if c.startswith("sudo cp "))
    assert str(unit_path) in cp
    assert res["unit"] == str(unit_path)


if __name__ == "__main__":
    # Standalone runner: execute the no-fixture tests directly; the fixtured one runs under pytest.
    simple = [test_render_fills_every_placeholder_from_explicit_values,
              test_user_comes_from_process_owner_not_USER_env,
              test_workdir_and_uvicorn_track_the_running_process]
    for fn in simple:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[service-setup] {len(simple)} tests PASS "
          f"(run `pytest tests/test_service_setup.py` for the install/monkeypatch test)")
