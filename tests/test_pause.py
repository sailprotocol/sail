"""
Pause / Resume / Stop — in-process, so the operator surface stays up and the state matches reality.

The dashboard is served BY the sail-host process, so pause must NOT `systemctl stop` it (that would
kill the surface that drives Resume, and self-stopping deadlocks systemd against the in-flight
request). Instead pause is a persisted flag: the daemon keeps running + reachable but stops serving
inference (503) and stops announcing; Resume clears it. phoenixd is untouched. These pin that.

Run standalone (`python tests/test_pause.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local", SAIL_OPERATOR_AUTOSTART="0")
import host.daemon as d  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _flag(tmp_path, monkeypatch):
    f = tmp_path / "paused"
    monkeypatch.setattr(d, "_PAUSE_FLAG", f)  # isolate from the real ~/.local/state/sail/paused
    return f


def test_pause_sets_flag_and_status_reports_paused(monkeypatch, tmp_path):
    f = _flag(tmp_path, monkeypatch)
    op = TestClient(d.operator_app)
    assert op.post("/api/control/pause").json() == {"ok": True, "paused": True}
    assert f.exists() and d._is_paused() is True          # in-process flag, not a stopped service
    assert op.get("/api/status").json()["paused"] is True  # status is authoritative for the dashboard


def test_resume_clears_flag(monkeypatch, tmp_path):
    _flag(tmp_path, monkeypatch)
    d._set_paused(True)
    monkeypatch.setattr(d.registry, "publish", lambda listing: {"success": ["local:x"], "failed": {}})
    op = TestClient(d.operator_app)
    body = op.post("/api/control/resume").json()
    assert body["ok"] is True and body["paused"] is False
    assert d._is_paused() is False
    assert op.get("/api/status").json()["paused"] is False


def test_inference_returns_503_only_while_paused(monkeypatch, tmp_path):
    _flag(tmp_path, monkeypatch)
    pub = TestClient(d.app)
    d._set_paused(True)
    r = pub.post("/v1/inference", json={"prompt": "hi", "max_tokens": 1})
    assert r.status_code == 503 and "paused" in r.text.lower()
    d._set_paused(False)
    r2 = pub.post("/v1/inference", json={"prompt": "hi", "max_tokens": 1})
    assert r2.status_code == 402  # back to the normal L402 challenge once resumed


def test_controls_are_local_only(monkeypatch, tmp_path):
    _flag(tmp_path, monkeypatch)
    op = TestClient(d.operator_app)
    for path in ("/api/control/pause", "/api/control/resume"):
        assert op.post(path, headers={"host": "abcd.onion"}).status_code == 404  # never over the onion


def test_remove_pauses_now_and_surfaces_only_the_manual_rm(monkeypatch, tmp_path):
    _flag(tmp_path, monkeypatch)
    called = {}
    monkeypatch.setattr(d, "_deferred_service_teardown", lambda *a, **k: called.setdefault("t", True))
    op = TestClient(d.operator_app)
    body = op.post("/api/control/remove", json={"confirm": "remove"}).json()
    assert body["ok"] is True and body["paused"] is True   # offline immediately (in-process)
    assert d._is_paused() is True and called.get("t") is True  # teardown deferred (no self-stop deadlock)
    assert any("rm /etc/systemd/system" in c for c in body["manual_commands"])
    # no per-request systemctl stop is surfaced as a blocking "run this" — the rm is the only manual step
    assert all("stop" not in c for c in body["manual_commands"])


def test_remove_requires_type_to_confirm(monkeypatch, tmp_path):
    _flag(tmp_path, monkeypatch)
    op = TestClient(d.operator_app)
    assert op.post("/api/control/remove", json={"confirm": "nope"}).status_code == 400


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
