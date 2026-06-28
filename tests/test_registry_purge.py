"""
Local-registry purge (cleanup): remove dev/test listings without touching real ones.

Run standalone (`python tests/test_registry_purge.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.listing import HostListing, ModelOffer  # noqa: E402


def _write(reg, pubkey, endpoint, model):
    reg.publish(HostListing(pubkey=pubkey, endpoint=endpoint,
                            models=[ModelOffer(name=model, price_msat_per_token=1000,
                                               context_window=8192)]))


def test_purge_stale_keeps_real():
    import shared.registry as registry
    with tempfile.TemporaryDirectory() as d:
        os.environ["REGISTRY_DIR"] = d
        os.environ["POW_TARGET"] = "0"  # skip mining for speed
        reg = registry.LocalRegistry()
        _write(reg, "ab" * 32, "http://realhost.onion", "qwen3:14b")        # real -> keep
        _write(reg, "cd" * 32, "http://127.0.0.1:8001", "qwen3:14b")        # localhost -> stale
        _write(reg, "host_deadbeef0000", "http://x.onion", "mock-echo:1b")  # host_ + mock -> stale
        assert len(list(pathlib.Path(d).glob("*.json"))) == 3

        r = registry.purge_local_registry(stale_only=True)
        assert r["removed"] == 2 and r["kept"] == 1, r
        left = list(pathlib.Path(d).glob("*.json"))
        assert len(left) == 1 and left[0].stem == "ab" * 32, left


def test_purge_all_wipes():
    import shared.registry as registry
    with tempfile.TemporaryDirectory() as d:
        os.environ["REGISTRY_DIR"] = d
        os.environ["POW_TARGET"] = "0"
        reg = registry.LocalRegistry()
        _write(reg, "ab" * 32, "http://realhost.onion", "qwen3:14b")
        r = registry.purge_local_registry(stale_only=False)
        assert r["removed"] == 1 and r["kept"] == 0, r
        assert list(pathlib.Path(d).glob("*.json")) == []


def test_purge_empty_dir_is_safe():
    import shared.registry as registry
    with tempfile.TemporaryDirectory() as d:
        os.environ["REGISTRY_DIR"] = os.path.join(d, "does-not-exist")
        r = registry.purge_local_registry()
        assert r == {"removed": 0, "kept": 0, "dir": os.path.join(d, "does-not-exist")}, r


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[registry-purge] {len(fns)} tests PASS")
