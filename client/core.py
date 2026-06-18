"""
Shared client core.

The discovery + reputation ranking, the L402 metered paying loop, token streaming, and Tor
routing live here so BOTH the CLI (`client/cli.py`) and the local web app (`client/webapp.py`)
call one implementation. Failures raise exceptions; `run_inference` turns them into structured
events so each frontend can present them its own way.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Iterator

import httpx

from shared import registry
from shared.l402 import NEXT_MARKER, DONE_MARKER
from host import moderation
from client import reputation
from client import wallet


def discover_hosts() -> list:
    """Discovered hosts (already PoW-filtered by registry), filtered to the client's model
    allowlist (if configured), then ranked by local reputation."""
    hosts = [h for h in registry.discover() if moderation.is_model_allowed(h.models[0].name)]
    return reputation.rank(hosts, reputation.load())


def proxy_for(endpoint: str) -> str | None:
    """Route .onion endpoints through Tor's SOCKS proxy; everything else connects directly.
    socks5h:// resolves the hostname (incl. .onion) via the proxy, as Tor requires."""
    if httpx.URL(endpoint).host.endswith(".onion"):
        return os.getenv("TOR_SOCKS", "socks5h://127.0.0.1:9050")
    return None


def lnd_pay(invoice: str) -> str:
    """Pay a BOLT11 invoice via this client's LND node (REST SendPaymentV2) -> preimage hex."""
    host = os.environ["LND_REST_HOST"].rstrip("/")
    cert = os.environ["LND_TLS_CERT_PATH"]
    macaroon = pathlib.Path(os.environ["LND_MACAROON_PATH"]).read_bytes().hex()
    body = {"payment_request": invoice, "timeout_seconds": 60,
            "fee_limit_msat": "10000", "no_inflight_updates": True}
    with httpx.stream("POST", f"{host}/v2/router/send", json=body,
                      headers={"Grpc-Metadata-macaroon": macaroon},
                      verify=cert, timeout=70.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if "error" in msg:
                raise RuntimeError(f"LND payment error: {msg['error']}")
            result = msg.get("result", {})
            status = result.get("status")
            if status == "SUCCEEDED":
                return result["payment_preimage"]
            if status == "FAILED":
                raise RuntimeError(f"payment failed: {result.get('failure_reason', 'unknown')}")
    raise RuntimeError("payment stream ended without a SUCCEEDED result")


def _run_async(coro_factory):
    """Run an async coroutine to completion in a dedicated thread with its own event loop, so
    these calls work from the sync CLI and from FastAPI's threadpool generator alike."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result()


_nwc = None  # cached Nwc client, so the wallet relay connection is reused across chunks


def nwc_pay(invoice: str) -> str:
    """Pay a BOLT11 invoice from the user's OWN wallet over NWC (NIP-47) -> preimage hex.
    Non-custodial: this only relays a pay_invoice request to the user's wallet."""
    global _nwc
    uri = wallet.load_uri()
    if not uri:
        raise RuntimeError("no wallet connected — connect one in the GUI or set NWC_URI")
    from nostr_sdk import Nwc, NostrWalletConnectUri, PayInvoiceRequest
    if _nwc is None:
        _nwc = Nwc(NostrWalletConnectUri.parse(uri))

    async def _go():
        return await _nwc.pay_invoice(PayInvoiceRequest(id=None, invoice=invoice, amount=None))

    return _run_async(_go).preimage


def pay_invoice(endpoint: str, ch: dict, proxy: str | None = None) -> str:
    """Obtain the preimage for a chunk challenge by paying. PAYMENTS=mock -> host /mock/pay shim;
    lnd -> the client's own LND node; nwc -> the user's own wallet via NWC (NIP-47)."""
    mode = os.getenv("PAYMENTS", "mock").lower()
    if mode == "lnd":
        return lnd_pay(ch["invoice"])      # client's own local LND, never via Tor
    if mode == "nwc":
        return nwc_pay(ch["invoice"])      # user's own wallet over NWC, never via Tor
    r = httpx.post(f"{endpoint}/mock/pay", json={"payment_hash": ch["payment_hash"]}, proxy=proxy)
    r.raise_for_status()
    return r.json()["preimage"]


def run_inference(host, prompt: str, max_tokens: int = 64) -> Iterator[dict]:
    """Run the full metered L402 exchange against `host`, yielding events:
        {"type":"token","text":str} | {"type":"done","spent_msat":int,"latency_ms":float}
        | {"type":"error","message":str}
    Records the outcome (success/latency or failure) against local reputation.
    """
    ep = host.endpoint
    proxy = proxy_for(ep)
    stream_timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
    t0 = time.monotonic()
    ok = False
    spent_msat = 0
    latency_ms = 0.0
    try:
        # Start a session: no creds -> the first chunk's 402 challenge.
        r = httpx.post(f"{ep}/v1/inference",
                       json={"prompt": prompt, "max_tokens": max_tokens}, proxy=proxy)
        if r.status_code != 402:
            raise RuntimeError(f"expected 402, got {r.status_code}: {r.text[:200]}")
        ch = r.json()

        # Pay each chunk, stream it, follow the trailer to the next chunk until done.
        while True:
            preimage = pay_invoice(ep, ch, proxy)
            spent_msat += ch["amount_msat"]
            auth = f"L402 {ch['macaroon']}:{preimage}"
            with httpx.stream("POST", f"{ep}/v1/inference",
                              json={"session_id": ch.get("session_id")},
                              headers={"Authorization": auth},
                              timeout=stream_timeout, proxy=proxy) as s:
                if s.status_code != 200:
                    raise RuntimeError(
                        f"chunk failed: {s.status_code} {s.read().decode(errors='replace')[:200]}")
                body = "".join(s.iter_text())  # one chunk is small; buffer to parse the trailer
            if DONE_MARKER in body:
                text = body.partition(DONE_MARKER)[0]
                if text:
                    yield {"type": "token", "text": text}
                break
            if NEXT_MARKER in body:
                text, _, meta = body.partition(NEXT_MARKER)
                if text:
                    yield {"type": "token", "text": text}
                ch = json.loads(meta)
                continue
            if body:  # no trailer (shouldn't happen) -> stop
                yield {"type": "token", "text": body}
            break

        ok = True
        latency_ms = (time.monotonic() - t0) * 1000
        yield {"type": "done", "spent_msat": spent_msat, "latency_ms": round(latency_ms, 1)}
    except httpx.TransportError as e:  # connect refused / Tor-unreachable / timeout / network drop
        yield {"type": "error", "kind": "unreachable", "message": str(e)}
    except Exception as e:  # noqa: BLE001 - surface any other failure as an event
        yield {"type": "error", "kind": "other", "message": str(e)}
    finally:
        reputation.record(host.pubkey, success=ok, latency_ms=(latency_ms if ok else None))
