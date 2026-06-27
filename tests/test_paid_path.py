"""
Paid-path reachability (host #2 "402 then nothing" investigation).

The host's first /v1/inference returned 402, then no paid re-request ever arrived. Two guards:
  (host) a paid re-request with a valid preimage for a phoenixd-style invoice MUST reach the serve
         loop — proving the receive backend isn't what rejects it (narrows the bug to client/transport);
  (client) nwc_pay must FAIL CLEANLY (clear error) when the wallet returns no preimage or hangs,
           instead of silently sending empty creds / never re-requesting (the stall the operator saw).

Run standalone (`python tests/test_paid_path.py`) or under pytest.
"""
from __future__ import annotations

import os
import pathlib
import secrets
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.update(PAYMENTS="mock", MODEL="mock", REGISTRY="local",
                  HOST_KEY_PATH="/tmp/sail-paidpath-host.nsec", LISTING_REANNOUNCE_SECONDS="0")
os.environ.pop("MODEL_ALLOWLIST", None)
os.environ.pop("MODEL_ALLOWLIST_PATH", None)

from shared.l402 import DONE_MARKER, sha256_hex  # noqa: E402


class _FakePhoenixd:
    """A phoenixd-style receive backend: mints an invoice carrying a real payment_hash; paying it
    reveals the matching preimage (which is all the host's L402 verify checks)."""
    def __init__(self):
        self._pre = secrets.token_bytes(32)
        self.preimage_hex = self._pre.hex()
        self.payment_hash = sha256_hex(self._pre)

    def create_invoice(self, amount_msat, expiry_seconds=None):
        return f"lnbcPHOENIXD{amount_msat}n1fake", self.payment_hash

    def is_settled(self, payment_hash_hex):
        return False

    def ping(self):
        return True, "ok"


def test_phoenixd_paid_request_reaches_serve_loop():
    from fastapi.testclient import TestClient
    from host.model import MockModel
    import host.daemon as d

    d._ln = _FakePhoenixd()   # host receives via (fake) phoenixd
    d._model = MockModel()    # explicit, so test order can't leave a failing model set
    c = TestClient(d.app)

    r = c.post("/v1/inference", json={"prompt": "hi", "max_tokens": 4})
    assert r.status_code == 402, r.status_code
    ch = r.json()
    assert ch["payment_hash"] == d._ln.payment_hash, "challenge must carry the phoenixd invoice hash"

    # Client pays the phoenixd invoice and reveals its preimage in the re-request.
    r2 = c.post("/v1/inference", json={"session_id": ch["session_id"]},
                headers={"Authorization": f"L402 {ch['macaroon']}:{d._ln.preimage_hex}"})
    body = r2.text
    assert r2.status_code == 200, "valid paid re-request must be accepted"
    assert "This is a mock" in body, "serve loop must run and stream tokens"  # 4 tokens @ max_tokens=4
    assert DONE_MARKER in body, "stream must complete cleanly"


def test_nwc_pay_fails_clean_on_missing_preimage():
    """A wallet that settles but returns no preimage must raise a clear error, not send empty creds
    (which would make the host reject the re-request — or worse, the client stall)."""
    from client import core, wallet
    orig_uri, orig_run, orig_nwc = wallet.load_uri, core._run_async, core._nwc
    try:
        wallet.load_uri = lambda: "nostr+walletconnect://abc?relay=wss://r&secret=00"
        core._nwc = object()  # non-None -> skip real Nwc construction
        core._run_async = lambda f: types.SimpleNamespace(preimage="")  # wallet returned no preimage
        try:
            core.nwc_pay("lnbc1fake")
            assert False, "expected a clean error"
        except RuntimeError as e:
            assert "no payment preimage" in str(e), str(e)
    finally:
        wallet.load_uri, core._run_async, core._nwc = orig_uri, orig_run, orig_nwc


def test_nwc_pay_fails_clean_on_timeout():
    import asyncio
    from client import core, wallet
    orig_uri, orig_run, orig_nwc = wallet.load_uri, core._run_async, core._nwc
    try:
        wallet.load_uri = lambda: "nostr+walletconnect://abc?relay=wss://r&secret=00"
        core._nwc = object()

        def _boom(f):
            raise asyncio.TimeoutError()
        core._run_async = _boom
        try:
            core.nwc_pay("lnbc1fake")
            assert False, "expected a clean error"
        except RuntimeError as e:
            assert "NWC payment failed" in str(e), str(e)
    finally:
        wallet.load_uri, core._run_async, core._nwc = orig_uri, orig_run, orig_nwc


def test_nwc_pay_premature_exit_gets_actionable_hint():
    """The opaque 'Generic: premature exit' (no NIP-47 response) must surface the real string AND a
    cause hint (relay flaky / phoenixd no-channel), not a bare 'premature exit'."""
    from client import core, wallet
    orig_uri, orig_run, orig_nwc = wallet.load_uri, core._run_async, core._nwc
    try:
        wallet.load_uri = lambda: "nostr+walletconnect://abc?relay=wss://r&secret=00"
        core._nwc = object()

        def _boom(f):
            raise Exception("Generic: premature exit")
        core._run_async = _boom
        try:
            core.nwc_pay("lnbc1fake")
            assert False
        except RuntimeError as e:
            m = str(e)
            assert "premature exit" in m, m          # the REAL error is preserved
            assert "phoenixd" in m and "channel" in m, m  # plus the actionable cause
    finally:
        wallet.load_uri, core._run_async, core._nwc = orig_uri, orig_run, orig_nwc


def test_nwc_pay_wallet_rejection_surfaced_verbatim():
    """A structured wallet rejection (NIP-47 code/message) must come through verbatim."""
    from client import core, wallet
    orig_uri, orig_run, orig_nwc = wallet.load_uri, core._run_async, core._nwc
    try:
        wallet.load_uri = lambda: "nostr+walletconnect://abc?relay=wss://r&secret=00"
        core._nwc = object()

        def _boom(f):
            raise Exception("PAYMENT_FAILED: no_route to destination")
        core._run_async = _boom
        try:
            core.nwc_pay("lnbc1fake")
            assert False
        except RuntimeError as e:
            assert "PAYMENT_FAILED: no_route to destination" in str(e), str(e)
    finally:
        wallet.load_uri, core._run_async, core._nwc = orig_uri, orig_run, orig_nwc


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[paid-path] {len(fns)} tests PASS")
