"""
Discovery freshness — dead/zombie host listings drop off (client-side stale filter + NIP-40 tag).

A host re-announces every ~300s; if it stops (dies/paused), its listing lingers on relays. The
client now hides listings whose newest re-announce (`updated_at`/`created_at`) is older than
LISTING_STALE_AFTER (~3× the heartbeat), and every published event carries a NIP-40 `expiration`
tag so relays that honor it purge stopped hosts too. Tested via the LOCAL registry (no relay mock).

Run standalone (`python tests/test_discovery_freshness.py`) or under pytest.
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.listing import HostListing, ModelOffer, LISTING_EXPIRY_SECONDS  # noqa: E402
from shared import registry  # noqa: E402


def _listing(pub: str, updated_at: int | None = None) -> HostListing:
    return HostListing(pubkey=pub, endpoint="http://x.onion",
                       models=[ModelOffer(name="llama3.2:3b", price_msat_per_token=1000, context_window=8192)],
                       updated_at=updated_at if updated_at is not None else int(time.time()))


# ---- NIP-40 expiration tag on every published listing ---------------------
def test_listing_carries_nip40_expiration_tag():
    now = 1_700_000_000
    ev = _listing("aa" * 32, updated_at=now).to_nostr_event()
    exp = [t for t in ev["tags"] if t and t[0] == "expiration"]
    assert exp, "listing must carry a NIP-40 expiration tag"
    assert exp[0][1] == str(now + LISTING_EXPIRY_SECONDS)  # relative to this listing's timestamp
    assert LISTING_EXPIRY_SECONDS > 300  # comfortably longer than the 300s re-announce heartbeat


# ---- client-side staleness filter (via the local registry) ----------------
def _local(monkeypatch, tmp_path, stale_after):
    monkeypatch.setenv("REGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("POW_TARGET", "0")           # skip PoW mining in the test
    monkeypatch.setenv("POW_MIN_DIFFICULTY", "0")
    monkeypatch.setenv("LISTING_STALE_AFTER", str(stale_after))
    return registry.LocalRegistry()


def test_stale_listing_is_hidden_fresh_is_shown(monkeypatch, tmp_path):
    reg = _local(monkeypatch, tmp_path, 900)
    now = int(time.time())
    reg.publish(_listing("aa" * 32, updated_at=now))            # fresh (just re-announced)
    reg.publish(_listing("bb" * 32, updated_at=now - 2000))     # dead (last beat 2000s ago > 900)
    pks = {h.pubkey for h in reg.discover()}
    assert "aa" * 32 in pks and "bb" * 32 not in pks, pks
    assert reg._stats["stale_hidden"] == 1


def test_stale_filter_disabled_with_zero(monkeypatch, tmp_path):
    reg = _local(monkeypatch, tmp_path, 0)  # 0 = never hide (opt-out)
    reg.publish(_listing("bb" * 32, updated_at=int(time.time()) - 99999))
    assert any(h.pubkey == "bb" * 32 for h in reg.discover())
    assert reg._stats["stale_hidden"] == 0


def test_freshness_boundary(monkeypatch, tmp_path):
    reg = _local(monkeypatch, tmp_path, 900)
    now = int(time.time())
    reg.publish(_listing("aa" * 32, updated_at=now - 800))   # within window -> shown
    reg.publish(_listing("bb" * 32, updated_at=now - 1000))  # past window -> hidden
    pks = {h.pubkey for h in reg.discover()}
    assert "aa" * 32 in pks and "bb" * 32 not in pks


if __name__ == "__main__":
    # standalone: run the no-fixture test; the monkeypatch/tmp_path ones need pytest
    test_listing_carries_nip40_expiration_tag()
    print("  ✓ test_listing_carries_nip40_expiration_tag")
    print("[discovery-freshness] 1 test PASS (run `pytest tests/test_discovery_freshness.py` for the rest)")
