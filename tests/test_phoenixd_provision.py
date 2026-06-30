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


# ---- seed/conf file permissions (the plaintext-seed backstop) -------------
def test_secure_seed_files_tightens_perms(tmp_path, monkeypatch):
    import os as _os
    d = tmp_path / ".phoenix"
    d.mkdir()
    seed = d / "seed.dat"; seed.write_text("word " * 12); seed.chmod(0o644)
    conf = d / "phoenix.conf"; conf.write_text("http-password=secret"); conf.chmod(0o644)
    monkeypatch.setattr(ps, "PHOENIX_DIR", d)
    monkeypatch.setattr(ps, "SEED_FILE", seed)
    monkeypatch.setattr(ps, "CONF_FILE", conf)
    ps.secure_seed_files()
    assert (seed.stat().st_mode & 0o777) == 0o600, oct(seed.stat().st_mode)
    assert (conf.stat().st_mode & 0o777) == 0o600, oct(conf.stat().st_mode)
    assert (d.stat().st_mode & 0o777) == 0o700, oct(d.stat().st_mode)


# ---- import an existing seed -----------------------------------------------
def test_import_seed_writes_seedfile_and_restores(tmp_path, monkeypatch):
    d = tmp_path / ".phoenix"
    seed = d / "seed.dat"
    conf = d / "phoenix.conf"
    monkeypatch.setattr(ps, "PHOENIX_DIR", d)
    monkeypatch.setattr(ps, "SEED_FILE", seed)
    monkeypatch.setattr(ps, "CONF_FILE", conf)
    monkeypatch.setattr(ps, "is_provisioned", lambda: False)  # no wallet yet
    words = ["abandon"] * 11 + ["about"]

    captured = {}

    def fake_provision(version=None):
        # phoenixd would read the seed.dat we wrote and "restore" — simulate by asserting it exists
        captured["seed_on_disk"] = seed.read_text()
        captured["mode"] = (seed.stat().st_mode & 0o777)
        return {"provisioned": True, "seed_words": words, "service": {"installed": False}}

    monkeypatch.setattr(ps, "provision", fake_provision)
    monkeypatch.setattr(ps, "read_seed_words", lambda: words)  # restore verification reads it back

    result = ps.import_seed(words)
    assert result["imported"] is True
    assert captured["seed_on_disk"] == "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    assert captured["mode"] == 0o600, oct(captured["mode"])  # secured before provision


def test_import_seed_refuses_when_wallet_exists_without_replace(monkeypatch):
    monkeypatch.setattr(ps, "is_provisioned", lambda: True)
    try:
        ps.import_seed(["abandon"] * 11 + ["about"])  # replace defaults False
    except RuntimeError as e:
        assert "replace=True" in str(e)
    else:
        raise AssertionError("import must refuse to clobber an existing wallet without replace")


def test_import_seed_replace_archives_then_restores(tmp_path, monkeypatch):
    d = tmp_path / ".phoenix"
    d.mkdir()
    (d / "seed.dat").write_text("zoo zoo zoo")  # an old (to-be-archived) wallet
    monkeypatch.setattr(ps, "PHOENIX_DIR", d)
    monkeypatch.setattr(ps, "SEED_FILE", d / "seed.dat")
    monkeypatch.setattr(ps, "CONF_FILE", d / "phoenix.conf")
    monkeypatch.setattr(ps, "is_provisioned", lambda: True)        # a wallet exists
    monkeypatch.setattr(ps, "_stop_phoenixd_service", lambda: None)
    monkeypatch.setattr(ps, "_port_in_use", lambda h, p: False)    # service stopped, port free
    archived = {}
    monkeypatch.setattr(ps, "_archive_phoenix_dir", lambda: archived.setdefault("done", d.parent / ".phoenix.replaced-1"))
    words = ["abandon"] * 11 + ["about"]
    monkeypatch.setattr(ps, "provision", lambda version=None: {"service": {}})
    monkeypatch.setattr(ps, "read_seed_words", lambda: words)
    out = ps.import_seed(words, replace=True)
    assert out["imported"] is True and "done" in archived  # old wallet archived, not blocked


def test_import_seed_replace_aborts_if_phoenixd_wont_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "is_provisioned", lambda: True)
    monkeypatch.setattr(ps, "_stop_phoenixd_service", lambda: None)
    monkeypatch.setattr(ps, "_port_in_use", lambda h, p: True)   # phoenixd still running
    archived = {"called": False}
    monkeypatch.setattr(ps, "_archive_phoenix_dir", lambda: archived.update(called=True))
    try:
        ps.import_seed(["abandon"] * 11 + ["about"], replace=True)
    except RuntimeError as e:
        assert "still in use" in str(e) and archived["called"] is False  # never archive a live wallet
    else:
        raise AssertionError("must abort (not archive) if phoenixd can't be stopped")


def test_wallet_funded_status(monkeypatch):
    import httpx
    monkeypatch.setattr(ps, "is_provisioned", lambda: True)
    monkeypatch.setattr(ps, "read_http_password", lambda: "pw")

    class FakeResp:
        def __init__(self, payload): self.status_code = 200; self._p = payload
        def json(self): return self._p

    def make_client(balance, channels, raise_err=False):
        class C:
            def __init__(self, **k): pass
            def get(self, path):
                if raise_err: raise httpx.ConnectError("refused")
                return FakeResp({"balanceSat": balance, "feeCreditSat": 0}) if path == "/getbalance" \
                    else FakeResp({"channels": channels})
        return C

    monkeypatch.setattr(httpx, "Client", make_client(0, []))
    assert ps.wallet_funded_status()[0] is False        # empty wallet
    monkeypatch.setattr(httpx, "Client", make_client(5000, []))
    assert ps.wallet_funded_status()[0] is True         # has balance
    monkeypatch.setattr(httpx, "Client", make_client(0, [{"channelId": "c"}]))
    assert ps.wallet_funded_status()[0] is True         # has a channel
    monkeypatch.setattr(httpx, "Client", make_client(0, [], raise_err=True))
    assert ps.wallet_funded_status()[0] is None         # unreachable -> unknown


def test_import_seed_detects_restore_mismatch(tmp_path, monkeypatch):
    d = tmp_path / ".phoenix"
    monkeypatch.setattr(ps, "PHOENIX_DIR", d)
    monkeypatch.setattr(ps, "SEED_FILE", d / "seed.dat")
    monkeypatch.setattr(ps, "CONF_FILE", d / "phoenix.conf")
    monkeypatch.setattr(ps, "is_provisioned", lambda: False)
    monkeypatch.setattr(ps, "provision", lambda version=None: {"service": {}})
    monkeypatch.setattr(ps, "read_seed_words", lambda: ["zoo"] * 12)  # phoenixd came up with a DIFFERENT wallet
    try:
        ps.import_seed(["abandon"] * 11 + ["about"])
    except RuntimeError as e:
        assert "mismatch" in str(e).lower()
    else:
        raise AssertionError("import must detect a wallet mismatch after restore")


# ---- endpoint: failure surfaces an error and gates the seed step -----------
def _client():
    from fastapi.testclient import TestClient
    import host.daemon as d
    return TestClient(d.operator_app)  # /api/setup/* lives on the localhost-only operator app


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
