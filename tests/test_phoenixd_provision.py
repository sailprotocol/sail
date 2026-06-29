"""
phoenixd provisioning failure surfacing + seed gating (dry-run finding F3).

A fresh operator picked phoenixd; provisioning never produced ~/.phoenix, yet the wizard
advanced and showed a 12-word seed for a wallet that was never created, then go-live failed
with a bare "phoenixd unreachable". Two contracts pinned here:

1. first_run surfaces the REAL cause (port already in use; phoenixd's own output on early exit)
   instead of a silent failure / bare exit code.
2. provision() only returns a seed for a wallet that actually persisted, and the /api/setup/payout
   endpoint returns an error (no seed) on failure so the wizard's seed ceremony is gated on
   provisioning success.

Run standalone (`python tests/test_phoenixd_provision.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import socket
import stat
import sys
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from host import phoenixd_setup as ps  # noqa: E402


@contextmanager
def _env(**kv):
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            os.environ[k] = v if v is not None else os.environ.get(k, "")
            if v is None:
                os.environ.pop(k, None)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _patch(obj, name, value):
    sentinel = object()
    old = getattr(obj, name, sentinel)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if old is sentinel:
            delattr(obj, name)
        else:
            setattr(obj, name, old)


# ---- first_run: clear cause instead of silent failure ----------------------
def test_first_run_refuses_when_api_port_already_in_use():
    """A second phoenixd can't bind the API port; instead of letting it exit early with the reason
    discarded, we pre-flight the port and say so (the same-box second-user failure)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        with _env(PHOENIXD_API_URL=f"http://127.0.0.1:{port}"), \
                _patch(ps, "is_provisioned", lambda: False):
            try:
                ps.first_run(pathlib.Path("/nonexistent/phoenixd"))
            except RuntimeError as e:
                assert "already in use" in str(e), e
                assert str(port) in str(e), e
            else:
                raise AssertionError("expected RuntimeError for port in use")
    finally:
        srv.close()


def test_first_run_surfaces_phoenixd_output_on_early_exit(tmp_path):
    """When phoenixd exits before writing a wallet, the error must include its output, not just a
    bare exit code (the swallowed-stderr diagnosability gap)."""
    fake = tmp_path / "phoenixd"
    fake.write_text("#!/bin/sh\necho 'fatal: could not start for some reason' 1>&2\nexit 7\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    # an almost-certainly-free port so the pre-flight passes and we reach the launch
    with _env(PHOENIXD_API_URL="http://127.0.0.1:9"), \
            _patch(ps, "is_provisioned", lambda: False):
        try:
            ps.first_run(fake, timeout=5)
        except RuntimeError as e:
            assert "exited early" in str(e), e
            assert "could not start for some reason" in str(e), e
        else:
            raise AssertionError("expected RuntimeError surfacing phoenixd output")


# ---- provision: strict success contract ------------------------------------
def test_provision_raises_if_wallet_not_persisted():
    """Even if first_run returns without raising, provision must NOT return a seed unless the wallet
    files actually exist — otherwise the wizard runs its seed-backup ceremony on nothing."""
    with _patch(ps, "download_phoenixd", lambda v=None: pathlib.Path("/bin/true")), \
            _patch(ps, "first_run", lambda b, **k: None), \
            _patch(ps, "is_provisioned", lambda: False):
        try:
            ps.provision()
        except RuntimeError as e:
            assert "incomplete" in str(e), e
        else:
            raise AssertionError("expected provision() to refuse an un-persisted wallet")


# ---- endpoint: failure surfaces an error and gates the seed step -----------
def _client():
    from fastapi.testclient import TestClient
    import host.daemon as d
    return TestClient(d.app)


def test_payout_endpoint_phoenixd_failure_returns_error_and_no_seed():
    from host import config_writer
    c = _client()
    with _patch(config_writer, "update_env_file", lambda *a, **k: None), \
            _patch(ps, "provision", _raise("phoenixd API port 127.0.0.1:9740 is already in use")):
        r = c.post("/api/setup/payout", json={"tier": "phoenixd"})
    assert r.status_code == 500, r.text
    body = r.json()
    assert "already in use" in body.get("error", ""), body
    assert "seed_words" not in body, body  # no seed ceremony for a node that wasn't created


def test_payout_endpoint_phoenixd_success_returns_seed():
    from host import config_writer
    seed = ["abandon"] * 12
    c = _client()
    fake = {"provisioned": True, "seed_words": seed, "service": {"installed": False}}
    with _patch(config_writer, "update_env_file", lambda *a, **k: None), \
            _patch(ps, "provision", lambda *a, **k: fake):
        r = c.post("/api/setup/payout", json={"tier": "phoenixd"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["tier"] == "phoenixd", body
    assert body["seed_words"] == seed, body


def _raise(msg):
    def _f(*a, **k):
        raise RuntimeError(msg)
    return _f


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v) and "tmp_path" not in v.__code__.co_varnames]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[phoenixd-provision] {len(fns)} tests PASS "
          f"(run `pytest tests/test_phoenixd_provision.py` for the tmp_path test too)")
