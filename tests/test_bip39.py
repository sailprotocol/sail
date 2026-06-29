"""BIP39 mnemonic validation (host wallet seed import)."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared import bip39  # noqa: E402

VALID12 = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def test_wordlist_is_canonical_2048():
    assert len(bip39.WORDLIST) == 2048
    assert bip39.WORDLIST[0] == "abandon" and bip39.WORDLIST[-1] == "zoo"


def test_valid_mnemonic_passes():
    ok, reason = bip39.validate_mnemonic(VALID12)
    assert ok and reason == "", reason


def test_bad_checksum_rejected():
    ok, reason = bip39.validate_mnemonic("abandon " * 12)  # valid words, wrong checksum
    assert not ok and "checksum" in reason.lower()


def test_unknown_word_rejected_without_leaking_phrase():
    ok, reason = bip39.validate_mnemonic("zzzz " * 12)
    assert not ok and "bip39" in reason.lower()
    assert "zzzz" in reason  # names the offending word, but not the whole phrase logic


def test_wrong_word_count_rejected():
    ok, reason = bip39.validate_mnemonic("abandon about")
    assert not ok and "12" in reason


def test_normalize_matches_phoenixd_regex():
    # phoenixd reads seed.dat via [a-z]+ — normalize must do the same (case-insensitive, punctuation-tolerant)
    assert bip39.normalize("  Abandon, ABANDON\nabout ") == ["abandon", "abandon", "about"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"\n[bip39] {len(fns)} tests PASS")
