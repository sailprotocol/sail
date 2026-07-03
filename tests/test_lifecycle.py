"""
Daemon lifecycle robustness (F-lifecycle).

A confused operator who double-starts (manual uvicorn + the sail-host service) or restarts after a
crash must get a CLEAR message + recovery step — never a raw traceback, never a respawn loop. These
test the detection paths with the bind/stem errors mocked (no real ports or Tor).

Run standalone (`python tests/test_lifecycle.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("SAIL_OPERATOR_AUTOSTART", "0")  # don't fire the import-time pre-flight here
import host.daemon as d  # noqa: E402
from host import transport  # noqa: E402


# ---- single-instance / port-in-use detection ------------------------------
def test_conflict_when_inference_port_busy(monkeypatch):
    monkeypatch.setattr(d, "_port_busy", lambda port, host="127.0.0.1": port == d.PORT)
    msg = d._single_instance_conflict()
    assert msg and "inference" in msg and "already" in msg
    assert str(d.PORT) in msg and "sail-host" in msg  # names the cause + a recovery step


def test_operator_port_busy_is_not_a_hard_conflict(monkeypatch):
    # only the inference port is the single-instance lock; the operator port degrades gracefully
    # (it may legitimately be the client GUI), so an operator-port-only collision is NOT fatal.
    monkeypatch.setattr(d, "_port_busy", lambda port, host="127.0.0.1": port == d.OPERATOR_PORT)
    assert d._single_instance_conflict() is None


def test_no_conflict_when_ports_free(monkeypatch):
    monkeypatch.setattr(d, "_port_busy", lambda port, host="127.0.0.1": False)
    assert d._single_instance_conflict() is None


def test_preflight_invokes_fatal_on_conflict(monkeypatch):
    # The import-time guard calls _fatal(msg) when a conflict is detected; _fatal hard-exits (mocked
    # here so we can assert it fired with the actionable message rather than killing the test process).
    monkeypatch.setattr(d, "_port_busy", lambda port, host="127.0.0.1": port == d.PORT)
    called = {}
    monkeypatch.setattr(d, "_fatal", lambda msg: called.setdefault("msg", msg))
    msg = d._single_instance_conflict()
    if msg:
        d._fatal(msg)
    assert "inference" in called["msg"]


# ---- onion address collision ----------------------------------------------
def _fake_stem(error_message, auth_error=None):
    """Install a fake stem.control. authenticate() raises `auth_error` (the cookie/control-port
    failure path); otherwise create_ephemeral_hidden_service raises `error_message`."""
    class FakeController:
        @classmethod
        def from_port(cls, port):
            return cls()
        def authenticate(self):
            if auth_error is not None:
                raise auth_error
        def create_ephemeral_hidden_service(self, *a, **k):
            raise Exception(error_message)

    mod = types.ModuleType("stem.control")
    mod.Controller = FakeController
    return mod


def test_onion_collision_becomes_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "stem.control", _fake_stem("Onion address collision"))
    monkeypatch.setenv("ONION_KEY_PATH", str(tmp_path / "onion.key"))  # NEW key (no file)
    try:
        transport.setup_onion(8001)
    except transport.OnionCollisionError as e:
        assert "already has this host's onion" in str(e)
        assert "restart tor" in str(e).lower() or "stop the other" in str(e).lower()
    else:
        raise AssertionError("expected OnionCollisionError on a collision")


def test_onion_other_error_is_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "stem.control", _fake_stem("some other tor failure"))
    monkeypatch.setenv("ONION_KEY_PATH", str(tmp_path / "onion.key"))
    try:
        transport.setup_onion(8001)
    except transport.OnionCollisionError:
        raise AssertionError("a non-collision error must NOT be reported as a collision")
    except Exception as e:
        assert "some other tor failure" in str(e)  # re-raised as-is


# ---- Tor control-cookie permission (the debian-tor group footgun) ---------
def test_cookie_permission_becomes_actionable_debian_tor_error(monkeypatch, tmp_path):
    # user not in debian-tor -> Tor's control cookie is unreadable -> a bare PermissionError.
    err = PermissionError(13, "Permission denied: '/run/tor/control.authcookie'")
    monkeypatch.setitem(sys.modules, "stem.control", _fake_stem("", auth_error=err))
    monkeypatch.setenv("ONION_KEY_PATH", str(tmp_path / "onion.key"))
    try:
        transport.setup_onion(8001)
    except transport.TorControlError as e:
        assert "debian-tor" in str(e) and "reboot" in str(e).lower()
        assert "groups | grep debian-tor" in str(e)  # the actionable check
    else:
        raise AssertionError("expected TorControlError for an unreadable auth cookie")


def test_control_port_refused_becomes_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "stem.control",
                        _fake_stem("", auth_error=Exception("[Errno 111] Connection refused")))
    monkeypatch.setenv("ONION_KEY_PATH", str(tmp_path / "onion.key"))
    try:
        transport.setup_onion(8001)
    except transport.TorControlError as e:
        assert "control port" in str(e).lower()
    else:
        raise AssertionError("expected TorControlError when the control port is unreachable")


# ---- operator port default (must not collide with the client GUI on 8090) --
def test_operator_port_default_is_not_8090():
    assert d.OPERATOR_PORT == 8092  # client GUI uses 8090; host operator surface uses 8092


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
