"""BOLT11 amount decoding (withdraw auto-fill)."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared import bolt11  # noqa: E402

# data part is bech32 (no '1' in its charset), so the last '1' is always the HRP separator
_TAIL = "qqqqqqqqqq"


def test_fixed_amount_micro():
    assert bolt11.decode_amount_msat("lnbc2500u1" + _TAIL) == 250_000_000  # 2500µBTC
    assert bolt11.decode_amount_sat("lnbc2500u1" + _TAIL) == 250_000


def test_fixed_amount_milli_nano():
    assert bolt11.decode_amount_sat("lnbc20m1" + _TAIL) == 2_000_000      # 20mBTC
    assert bolt11.decode_amount_sat("lnbc100n1" + _TAIL) == 10            # 100nBTC = 10 sat


def test_testnet_prefix():
    assert bolt11.decode_amount_sat("lntb500u1" + _TAIL) == 50_000


def test_any_amount_invoice_is_none():
    assert bolt11.decode_amount_msat("lnbc1" + _TAIL) is None   # no amount in the HRP
    assert bolt11.decode_amount_sat("lnbc1" + _TAIL) is None


def test_case_insensitive_and_whitespace():
    assert bolt11.decode_amount_sat("  LNBC2500U1" + _TAIL.upper() + "  ") == 250_000


def test_garbage_returns_none():
    for bad in ("", "hello", "bc1qxyz", "not-an-invoice", "ln"):
        assert bolt11.decode_amount_msat(bad) is None, bad


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"\n[bolt11] {len(fns)} tests PASS")
