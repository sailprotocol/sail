"""
Minimal BOLT11 amount decoder — read a fixed invoice amount from the human-readable part, locally.

The withdraw flow only needs the AMOUNT (to pre-fill + lock the amount field when the invoice
specifies one). That amount is encoded in the BOLT11 human-readable part (HRP), so we can read it
with simple string parsing — no bech32 data decode, no external service, no phoenixd round-trip.

HRP format: `ln` + currency (bc/tb/bcrt/…) + optional amount (digits + optional multiplier m/u/n/p),
then the bech32 separator `1`, then the data. e.g. `lnbc2500u1...` → 2500 micro-BTC = 250_000 sat.
Returns the amount in MSAT, or None for a zero/any-amount invoice (no amount in the HRP) or anything
we can't confidently parse (caller then leaves the field editable).
"""
from __future__ import annotations

# Currency prefixes, longest-first so 'bcrt' matches before 'bc' and 'tbs' before 'tb'.
_CURRENCIES = ("bcrt", "tbs", "tb", "bc", "sb")
# msat per 1 unit of (digits × multiplier). 1 BTC = 100_000_000_000 msat.
_MULT_MSAT = {"": 100_000_000_000, "m": 100_000_000, "u": 100_000, "n": 100}


def decode_amount_msat(invoice: str) -> int | None:
    s = (invoice or "").strip().lower()
    if not s.startswith("ln"):
        return None
    sep = s.rfind("1")  # bech32 separator is the LAST '1' (amount digits may contain '1')
    if sep <= 0:
        return None
    rest = s[2:sep]  # HRP without the 'ln' prefix, e.g. 'bc2500u'
    cur = next((c for c in _CURRENCIES if rest.startswith(c)), None)
    if cur is None:
        return None
    amt = rest[len(cur):]
    if amt == "":
        return None  # no amount -> any-amount invoice (leave the field editable)
    mult = "" if amt[-1].isdigit() else amt[-1]
    num = amt if mult == "" else amt[:-1]
    if not num.isdigit() or num == "":
        return None
    n = int(num)
    if mult == "p":  # pico-BTC = 0.1 msat; only valid when a multiple of 10
        return n // 10 if n % 10 == 0 else None
    per = _MULT_MSAT.get(mult)
    return None if per is None else n * per


def decode_amount_sat(invoice: str) -> int | None:
    """Fixed amount in whole sats (floor), or None for any-amount / unparseable."""
    msat = decode_amount_msat(invoice)
    return None if msat is None else msat // 1000
