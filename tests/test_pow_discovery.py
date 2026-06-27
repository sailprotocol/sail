"""
PoW discovery tests — guard the host #2 "hidden by PoW" class of bug.

The core invariant: a listing GROUND at difficulty N must be ACCEPTED by a client whose
POW_MIN_DIFFICULTY <= N (host and client measure the same bits over the same bytes), and the
discover() rejection accounting must be ACCURATE (a low-PoW listing is bucketed as PoW with its
measured-vs-required bits; a parse failure is not mislabeled as PoW).

Run standalone (`python tests/test_pow_discovery.py`) or under pytest.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.listing import HostListing, ModelOffer, LISTING_KIND  # noqa: E402
from shared.pow import leading_zero_bits  # noqa: E402


def _listing(pubkey="aa" * 32):
    return HostListing(pubkey=pubkey, endpoint="http://x.onion",
                       models=[ModelOffer(name="llama3.2:3b", price_msat_per_token=1000,
                                          context_window=8192)])


def _local_registry(tmp, target, floor):
    """A fresh LocalRegistry with POW env set (constructed directly to dodge the module cache)."""
    os.environ["REGISTRY_DIR"] = str(tmp)
    os.environ["POW_TARGET"] = str(target)
    os.environ["POW_MIN_DIFFICULTY"] = str(floor)
    from shared.registry import LocalRegistry
    return LocalRegistry()


def test_ground_at_N_accepted_by_floor_below_N():
    """The reported impossibility: ground at 16, a floor of 8 must accept it."""
    with tempfile.TemporaryDirectory() as d:
        reg = _local_registry(d, target=16, floor=8)
        reg.publish(_listing())
        hosts = reg.discover()
        assert len(hosts) == 1, f"ground-16 listing rejected by floor 8: {reg._stats}"
        assert reg._stats["pow_rejected"] == 0


def test_ground_at_N_accepted_by_floor_equal_N():
    with tempfile.TemporaryDirectory() as d:
        reg = _local_registry(d, target=12, floor=12)
        reg.publish(_listing())
        assert len(reg.discover()) == 1, reg._stats


def test_low_pow_rejected_and_bucketed_with_measured_bits():
    """A listing ground below the floor is hidden, counted as PoW (not sig/parse), and reports
    measured<required so --list is diagnosable."""
    with tempfile.TemporaryDirectory() as d:
        reg = _local_registry(d, target=4, floor=20)  # ground low, demand high
        reg.publish(_listing("bb" * 32))
        hosts = reg.discover()
        assert hosts == []
        assert reg._stats["pow_rejected"] == 1
        assert reg._stats["sig_rejected"] == 0 and reg._stats["parse_rejected"] == 0
        hid = reg._stats["pow_hidden"]
        assert len(hid) == 1 and hid[0]["required"] == 20
        assert hid[0]["bits"] < 20 and hid[0]["pubkey"] == "bb" * 32


def test_parse_error_not_mislabeled_as_pow():
    """A corrupt listing file is a parse error, never counted as 'by PoW'."""
    with tempfile.TemporaryDirectory() as d:
        reg = _local_registry(d, target=8, floor=8)
        (pathlib.Path(d) / "garbage.json").write_text("{not json")
        reg.discover()
        assert reg._stats["parse_rejected"] == 1
        assert reg._stats["pow_rejected"] == 0


def test_nostr_grind_measured_consistently():
    """The REAL host path: a host grinding N via nostr-sdk builder.pow(N) produces an event whose
    id the client measures at >= N bits (over a relay JSON round-trip). This is what makes a
    ground-16 host pass an 8-bit floor — host #1 works, host #2's failure was zero-PoW."""
    from nostr_sdk import EventBuilder, Keys, Kind, Tag, Event
    keys = Keys.generate()
    ev = _listing(keys.public_key().to_hex()).to_nostr_event()
    N = 12
    signed = (EventBuilder(Kind(LISTING_KIND), ev["content"])
              .tags([Tag.parse(t) for t in ev["tags"]]).pow(N).sign_with_keys(keys))
    # round-trip through JSON as a relay would deliver it, then measure like the client does
    received = Event.from_json(signed.as_json())
    assert received.verify()
    bits = leading_zero_bits(received.id().to_hex())
    assert bits >= N, f"ground at {N} but client measured {bits}"
    # and an UN-ground event carries no nonce tag (the host #2 / crooked-caliber signature)
    plain = (EventBuilder(Kind(LISTING_KIND), ev["content"])
             .tags([Tag.parse(t) for t in ev["tags"]]).sign_with_keys(keys))
    tags = [t.as_vec() for t in plain.tags().to_vec()]
    assert not any(t and t[0] == "nonce" for t in tags), "un-ground event should have no nonce tag"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[pow-discovery] {len(fns)} tests PASS")
