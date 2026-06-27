"""
Host identity tests — the persisted-file identity path must behave identically to the env-var
path (host #2 publishes via the file, host #1 via NOSTR_HOST_NSEC; they must sign the same).

Guards the discovery bug where host #2's listing never landed: both paths must yield the same
pubkey and a byte-identical, validly-signed kind-38111 event for the same inputs.

Run standalone (`python tests/test_identity.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nostr_sdk import EventBuilder, Keys, Kind, Tag, Timestamp  # noqa: E402
from shared import identity  # noqa: E402
from shared.listing import LISTING_KIND  # noqa: E402

_FIXED = Timestamp.from_secs(1_782_600_000)  # fix created_at so two signings yield the same id


@contextmanager
def _env(**kw):
    """Set env vars (None deletes) and restore them afterwards, so tests don't leak identity."""
    keys = ("NOSTR_HOST_NSEC", "HOST_KEY_PATH")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in kw.items():
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


def _signed(keys):
    return (EventBuilder(Kind(LISTING_KIND), "listing-content")
            .tags([Tag.parse(["d", keys.public_key().to_hex()]),
                   Tag.parse(["n", "inference-net-v0"])])
            .custom_created_at(_FIXED)
            .sign_with_keys(keys))


def test_file_and_env_paths_sign_identically():
    nsec = Keys.generate().secret_key().to_bech32()
    with _env(NOSTR_HOST_NSEC=nsec, HOST_KEY_PATH=None):
        ek = identity.host_keys()
        e_env = _signed(ek)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "host.nsec")
        open(path, "w").write(nsec)
        with _env(NOSTR_HOST_NSEC=None, HOST_KEY_PATH=path):
            fk = identity.host_keys()
            e_file = _signed(fk)

    assert ek.public_key().to_hex() == fk.public_key().to_hex(), "pubkeys differ"
    assert e_env.id().to_hex() == e_file.id().to_hex(), "event ids differ for identical inputs"
    assert e_env.verify() and e_file.verify(), "a path produced an invalid signature"
    # the event's author must be the signing key (not a stale/placeholder pubkey)
    assert e_env.author().to_hex() == ek.public_key().to_hex()
    assert e_file.author().to_hex() == fk.public_key().to_hex()


def test_first_run_generates_persists_and_is_stable():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "host.nsec")
        with _env(NOSTR_HOST_NSEC=None, HOST_KEY_PATH=path):
            pk1 = identity.host_pubkey_hex()          # first run: generate + persist
            assert pathlib.Path(path).exists(), "key file not persisted"
            assert oct(pathlib.Path(path).stat().st_mode)[-3:] == "600", "key file not chmod 600"
            assert open(path).read().strip().startswith("nsec1"), "not a real nsec"
            pk2 = identity.host_pubkey_hex()          # reload: identical pubkey
            assert pk1 == pk2, "pubkey changed across reloads"
            assert not pk1.startswith("host_"), "must be a real key, not a placeholder"


def test_env_overrides_file():
    nsec = Keys.generate().secret_key().to_bech32()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "host.nsec")
        open(path, "w").write(Keys.generate().secret_key().to_bech32())  # different file key
        with _env(NOSTR_HOST_NSEC=nsec, HOST_KEY_PATH=path):
            assert identity.host_pubkey_hex() == Keys.parse(nsec).public_key().to_hex()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[identity] {len(fns)} tests PASS")
