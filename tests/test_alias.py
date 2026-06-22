"""
Tests for shared.alias — the frozen wordlists + deterministic derivation.

Run standalone (`python tests/test_alias.py`) or under pytest. These guard the two ways host and
client could silently diverge: the wordlists changing, and the derivation drifting.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.alias import ADJECTIVES, NOUNS, derive_alias, alias_label  # noqa: E402

# Pinned pubkey -> alias vectors. If the lists or their order ever change, these fail loudly.
# (Generated from the shipped shared/alias.py; this is the locked contract.)
VECTORS = [
    ("2a2f1c3100000000000000000000000000000000000000000000000000000000", "extended-atom"),
    ("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", "girly-sultan"),
    ("0000000000000000000000000000000000000000000000000000000000000000", "lifelong-pentagon"),
    ("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "steamy-ministry"),
    ("deadbeef11111111111111111111111111111111111111111111111111111111", "deprived-carriage"),
]


def test_wordlist_lengths():
    assert len(ADJECTIVES) == 1024, len(ADJECTIVES)
    assert len(NOUNS) == 1024, len(NOUNS)


def test_no_duplicates_within_or_across():
    assert len(set(ADJECTIVES)) == 1024, "duplicate adjective(s)"
    assert len(set(NOUNS)) == 1024, "duplicate noun(s)"
    overlap = set(ADJECTIVES) & set(NOUNS)
    assert not overlap, f"lists must be disjoint; shared: {sorted(overlap)[:10]}"


def test_wordlist_charset():
    for w in (*ADJECTIVES, *NOUNS):
        assert re.fullmatch(r"[a-z]{4,8}", w), f"non-clean word: {w!r}"


def test_pinned_vectors():
    for pubkey, expected in VECTORS:
        assert derive_alias(pubkey) == expected, f"{pubkey[:12]} -> {derive_alias(pubkey)} != {expected}"


def test_label_format():
    assert alias_label("2a2f1c31" + "00" * 28) == "extended-atom · 2a2f"


def test_hashes_raw_bytes_not_hex_string():
    """The most common host/client drift bug: hashing the hex string instead of the 32 raw bytes.
    Lock that the derivation uses the decoded bytes."""
    pk = "2a2f1c3100000000000000000000000000000000000000000000000000000000"
    n_bytes = int.from_bytes(hashlib.sha256(bytes.fromhex(pk)).digest()[:4], "big")
    n_hexstr = int.from_bytes(hashlib.sha256(pk.encode()).digest()[:4], "big")
    assert n_bytes != n_hexstr, "test setup"  # they really do differ
    expect = f"{ADJECTIVES[(n_bytes >> 10) & 0x3FF]}-{NOUNS[n_bytes & 0x3FF]}"
    assert derive_alias(pk) == expect, "derive_alias must hash raw bytes, not the hex string"


def test_determinism_and_purity():
    pk = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    assert derive_alias(pk) == derive_alias(pk)  # pure: same input -> same output


def test_single_source_no_duplicate_implementation():
    """Anti-drift rule: derive_alias is defined in exactly ONE place (shared/alias.py). Host and
    client must import it, never re-implement it (a JS or second-Python copy would drift)."""
    root = pathlib.Path(__file__).resolve().parent.parent
    defs = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root))
        # Skip the venv, tests, and tools/ — the one-time generator embeds the function as a
        # template string that PRODUCED shared/alias.py (provenance, not a runtime second copy).
        if "/.venv/" in str(p) or rel.startswith(("tools/", "tests/")):
            continue
        if re.search(r"^\s*def\s+derive_alias\b", p.read_text(), re.M):
            defs.append(rel)
    assert defs == ["shared/alias.py"], f"derive_alias defined in: {defs}"


def test_client_derives_from_pubkey_ignoring_claimed_alias():
    """Anti-impersonation: the alias is a pure function of the pubkey, so a host that *claims*
    another's name can't get it — the derived value is whatever falls out of its own key."""
    impersonator_pk = "deadbeef11111111111111111111111111111111111111111111111111111111"
    claimed = "trusted-bank"  # what a malicious listing might stuff in
    assert derive_alias(impersonator_pk) != claimed
    assert derive_alias(impersonator_pk) == "deprived-carriage"  # its real, derived name


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[alias] {len(fns)} tests PASS")
