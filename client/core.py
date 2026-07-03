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
import sys
import time
from typing import Iterator

import httpx

from shared import registry
from shared.l402 import NEXT_MARKER, DONE_MARKER, ERROR_MARKER
from host import moderation
from client import reputation
from client import wallet


def _clog(msg: str) -> None:
    """Step trace to stderr (keeps stdout clean for streamed tokens) so the paid path is
    diagnosable: which step ran, where it stalled."""
    print(f"[client] {msg}", file=sys.stderr, flush=True)


class PaymentConfigError(RuntimeError):
    """The client can't pay this host because of a CLIENT-side config mismatch (e.g. PAYMENTS=mock
    against a real-wallet host), not a host fault. Surfaced clearly and NOT charged to the host's
    local reputation."""


def discover_hosts() -> list:
    """Discovered hosts (already PoW-filtered by registry), filtered to the client's model
    allowlist (if configured), then ranked by local reputation. Pure discovery — never pays."""
    hosts = [h for h in registry.discover() if moderation.is_model_allowed(h.models[0].name)]
    return reputation.rank(hosts, reputation.load())


def discover_hosts_detailed() -> dict:
    """Like discover_hosts() but also reports what was filtered out, so `--list` can show it
    instead of a bare 'No hosts found'. Never touches a pay path."""
    raw = registry.discover()
    stats = registry.discovery_stats()
    rep = reputation.load()
    allowed = [h for h in raw if moderation.is_model_allowed(h.models[0].name)]
    kept, hidden = reputation.partition(allowed, rep)
    rep_hidden_detail = [{"pubkey": h.pubkey, **reputation.hidden_reason(rep.get(h.pubkey))}
                         for h in hidden]  # why each is down + when it clears (for --list)
    return {"hosts": kept, "rep_hidden": len(hidden), "rep_hidden_detail": rep_hidden_detail,
            "allowlist_hidden": len(raw) - len(allowed),
            "pow_rejected": stats.get("pow_rejected", 0),
            "pow_hidden": stats.get("pow_hidden", []),       # [{pubkey, bits, required}]
            "sig_rejected": stats.get("sig_rejected", 0),
            "parse_rejected": stats.get("parse_rejected", 0),
            "stale_hidden": stats.get("stale_hidden", 0)}    # dead hosts that stopped re-announcing


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


def reset_nwc_client() -> None:
    """Drop the cached Nwc client so the next payment rebuilds it from the CURRENT NWC_URI. Must be
    called whenever the wallet connection changes (connect/disconnect) — otherwise a reconnect would
    silently keep paying from the old wallet."""
    global _nwc
    _nwc = None


NWC_PAY_TIMEOUT = float(os.getenv("NWC_PAY_TIMEOUT", "120"))  # cap so a stuck wallet/relay fails clean


def _nwc_failure_hint(detail: str) -> str:
    """Turn nostr-sdk's opaque NWC error into something actionable. 'premature exit'/timeout means
    NO usable NIP-47 response came back (vs the wallet replying with a real rejection code)."""
    d = detail.lower()
    if "premature" in d or "timeout" in d or "timed out" in d or "generic" in d:
        return (f"no NIP-47 response came back ({detail}) — the wallet/relay didn't reply. Run "
                "`python -m client.cli --nwc-check` to test the link (is the relay reachable? does "
                "the wallet answer get_info?). Likely causes: a flaky/wrong NWC relay, the wallet "
                "not connected to it, or the wallet not actually attempting the payment — check the "
                "wallet's own history for this attempt. (Far rarer: a host whose phoenixd has no "
                "channel yet.)")
    return f"the wallet rejected it: {detail}"  # carries the NIP-47 code/message verbatim


def nwc_describe() -> dict:
    """Best-effort: the wallet pubkey + relay(s) the NWC connection uses, for diagnostics. Never
    raises (parse failures just yield {connected: ...})."""
    uri = wallet.load_uri()
    if not uri:
        return {"connected": False}
    try:
        from nostr_sdk import NostrWalletConnectUri
        p = NostrWalletConnectUri.parse(uri)
        return {"connected": True, "wallet_pubkey": p.public_key().to_hex(),
                "relays": [str(r) for r in p.relays()]}
    except Exception as e:  # noqa: BLE001
        return {"connected": True, "wallet_pubkey": None, "relays": [], "parse_error": str(e)[:120]}


def _nwc_client():
    """The cached Nwc client (built with a generous response window so a slow wallet reply isn't
    cut off as a 'premature exit')."""
    global _nwc
    if _nwc is None:
        from datetime import timedelta
        from nostr_sdk import Nwc, NostrWalletConnectUri, NostrWalletConnectOptions
        uri = wallet.load_uri()
        if not uri:
            raise RuntimeError("no wallet connected — connect one in the GUI or set NWC_URI")
        opts = NostrWalletConnectOptions().timeout(timedelta(seconds=NWC_PAY_TIMEOUT))
        _nwc = Nwc.with_opts(NostrWalletConnectUri.parse(uri), opts)
    return _nwc


def _nwc_fetch_info_event(relays: list, wallet_pubkey_hex: str | None) -> dict:
    """Read the wallet's NIP-47 info event (kind 13194) straight off the relay — it's PLAINTEXT
    (space-separated supported methods + a notifications tag), so it sidesteps the SDK's strict
    typed get_info deserialize that chokes on newer methods/notifications. {found, methods, ...}."""
    from datetime import timedelta
    from nostr_sdk import Client, Filter, Kind, RelayUrl
    if not relays or not wallet_pubkey_hex:
        return {"found": False, "error": "no relay/pubkey"}

    async def _go():
        c = Client()
        for r in relays:
            await c.add_relay(RelayUrl.parse(r))
        await c.connect()
        try:
            await c.wait_for_connection(timedelta(seconds=10))
        except Exception:  # noqa: BLE001
            pass
        evs = await c.fetch_events(Filter().kind(Kind(13194)), timedelta(seconds=10))
        out = evs.to_vec()
        await c.shutdown()
        return out

    try:
        evs = _run_async(_go)
    except Exception as e:  # noqa: BLE001
        return {"found": False, "error": f"{type(e).__name__}: {e}"}
    # filter to our wallet in Python (robust regardless of relay-side author filtering)
    ours = [e for e in evs if e.author().to_hex() == wallet_pubkey_hex]
    if not ours:
        return {"found": False}
    ev = ours[0]
    methods = sorted({m for m in ev.content().split() if m})
    notifs = []
    for t in ev.tags().to_vec():
        v = t.as_vec()
        if len(v) >= 2 and v[0] == "notifications":
            notifs = v[1].split()
    return {"found": True, "methods": methods, "notifications": notifs}


def nwc_check() -> dict:
    """Probe the NWC link WITHOUT paying. PRIMARY: read the wallet's plaintext kind-13194 info
    event off the relay (robust — no strict typed deserialize). FALLBACK: a get_info round-trip,
    where a *deserialize* failure still means the wallet RESPONDED (the SDK just can't map newer
    fields) and must NOT be reported as 'no response'. Only a real timeout/no-event is a failure."""
    import asyncio
    d = nwc_describe()
    if not d.get("connected"):
        return {**d, "ok": False, "detail": "no wallet connected (set NWC_URI or connect in the GUI)"}

    info = _nwc_fetch_info_event(d.get("relays"), d.get("wallet_pubkey"))
    if info.get("found"):
        return {**d, "ok": True, "methods": info.get("methods", []),
                "notifications": info.get("notifications", []), "source": "info-event(13194)"}

    try:
        resp = _run_async(lambda: asyncio.wait_for(_nwc_client().get_info(), NWC_PAY_TIMEOUT))
        methods = sorted(str(m).split(".")[-1].lower() for m in resp.methods)
        return {**d, "ok": True, "methods": methods, "source": "get_info"}
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        low = detail.lower()
        if any(s in low for s in ("deserialize", "unknown method", "unknown notification")):
            # The wallet REPLIED; the SDK just couldn't strictly type newer fields. That's a SUCCESS,
            # not a dead wallet — the false-negative this fixes.
            return {**d, "ok": True, "methods": [], "source": "get_info(unparsed)",
                    "note": f"wallet responded but the SDK couldn't fully parse it ({detail})"}
        return {**d, "ok": False, "detail": detail}


def nwc_pay(invoice: str) -> str:
    """Pay a BOLT11 invoice from the user's OWN wallet over NWC (NIP-47) -> preimage hex.
    Non-custodial: this only relays a pay_invoice request to the user's wallet."""
    import asyncio
    from nostr_sdk import PayInvoiceRequest
    nwc = _nwc_client()
    desc = nwc_describe()  # log the request we're about to send, so a stall is attributable
    _clog(f"NWC pay request -> wallet={(desc.get('wallet_pubkey') or '?')[:8]} "
          f"relays={desc.get('relays')} invoice={invoice[:20]}…")

    async def _go():
        # Hard cap (slightly over the NWC timeout): a hung payment used to make the client never
        # re-request, so the host only saw the 402 and the operator saw a silent stall.
        return await asyncio.wait_for(
            nwc.pay_invoice(PayInvoiceRequest(id=None, invoice=invoice, amount=None)),
            NWC_PAY_TIMEOUT + 10)

    try:
        resp = _run_async(_go)
    except Exception as e:  # noqa: BLE001 — capture the REAL error (untruncated) and explain it
        detail = f"{type(e).__name__}: {e}"
        _clog(f"NWC pay_invoice FAILED: {detail}")  # full error to stderr for diagnosis
        raise RuntimeError("NWC payment failed — " + _nwc_failure_hint(detail)) from e
    preimage = getattr(resp, "preimage", None)
    if not preimage:
        # Some wallets settle but return no preimage — we then can't prove payment to the host, so
        # the metered (preimage-reveal) flow can't continue. Surface it instead of sending empty creds.
        raise RuntimeError("NWC wallet returned no payment preimage — this wallet can't be used for "
                           "metered streaming (try the BOLT11 option)")
    return preimage


def pay_invoice(endpoint: str, ch: dict, proxy: str | None = None) -> str:
    """Obtain the preimage for a chunk challenge by paying. PAYMENTS=mock -> host /mock/pay shim;
    lnd -> the client's own LND node; nwc -> the user's own wallet via NWC (NIP-47)."""
    mode = os.getenv("PAYMENTS", "mock").lower()
    if mode == "lnd":
        return lnd_pay(ch["invoice"])      # client's own local LND, never via Tor
    if mode == "nwc":
        return nwc_pay(ch["invoice"])      # user's own wallet over NWC, never via Tor
    r = httpx.post(f"{endpoint}/mock/pay", json={"payment_hash": ch["payment_hash"]}, proxy=proxy)
    if r.status_code == 404:
        # Real hosts (phoenixd/LND) have no /mock/pay route — this is a mock-vs-real MISMATCH on
        # OUR side, not a host failure. Surface it clearly; don't blame the host's reputation.
        raise PaymentConfigError(
            "this host needs a real wallet (NWC or LND) — you're in PAYMENTS=mock mode")
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
    # httpx's read timeout is the max gap between successive network reads — it RESETS each time
    # bytes arrive, so a slow-but-steady stream never trips it; the budget just has to cover a
    # cold first token + a Tor/payment round-trip. qwen3:14b on a 3060 over Tor is ~0.6 tok/s.
    read_to = float(os.getenv("CLIENT_READ_TIMEOUT", "300"))
    stream_timeout = httpx.Timeout(connect=15.0, read=read_to, write=15.0, pool=15.0)
    t0 = time.monotonic()
    ok = False
    penalize = True  # a CLIENT-side config error (mock-vs-real) must not bury the host's reputation
    spent_msat = 0
    latency_ms = 0.0
    mode = os.getenv("PAYMENTS", "mock").lower()
    try:
        # Start a session: no creds -> the first chunk's 402 challenge.
        r = httpx.post(f"{ep}/v1/inference",
                       json={"prompt": prompt, "max_tokens": max_tokens}, proxy=proxy)
        if r.status_code != 402:
            raise RuntimeError(f"expected 402, got {r.status_code}: {r.text[:200]}")
        ch = r.json()
        _clog(f"got 402 challenge hash={ch.get('payment_hash','')[:8]} "
              f"amount={ch.get('amount_msat')}msat — paying via {mode}")

        # Pay each chunk, stream it, follow the trailer to the next chunk until done.
        while True:
            # Payment is the CLIENT's side (wallet / NWC relay / route). A failure here is NOT the
            # host's fault, so we bail WITHOUT recording against the host's reputation (penalize off)
            # — a dead wallet must never bury an innocent host.
            try:
                preimage = pay_invoice(ep, ch, proxy)
            except PaymentConfigError as e:   # mock-vs-real mismatch (our config)
                penalize = False
                yield {"type": "error", "kind": "config", "message": str(e)}
                return
            except Exception as e:            # noqa: BLE001 — wallet/relay/route payment failure
                penalize = False
                yield {"type": "error", "kind": "payment_failed", "message": str(e)}
                return
            spent_msat += ch["amount_msat"]
            _clog(f"paid (preimage_len={len(preimage or '')}) — re-requesting chunk over "
                  f"{'tor' if proxy else 'direct'}")
            auth = f"L402 {ch['macaroon']}:{preimage}"
            with httpx.stream("POST", f"{ep}/v1/inference",
                              json={"session_id": ch.get("session_id")},
                              headers={"Authorization": auth},
                              timeout=stream_timeout, proxy=proxy) as s:
                if s.status_code != 200:
                    raise RuntimeError(
                        f"chunk failed: {s.status_code} {s.read().decode(errors='replace')[:200]}")
                body = "".join(s.iter_text())  # one chunk is small; buffer to parse the trailer
            trailer = ("ERROR" if ERROR_MARKER in body else "DONE" if DONE_MARKER in body
                       else "NEXT" if NEXT_MARKER in body else "none")
            _clog(f"chunk received ({len(body)}B, trailer={trailer})")
            if ERROR_MARKER in body:
                # Host failed to serve AFTER we paid — clean typed end, not a silent cut.
                text, _, meta = body.partition(ERROR_MARKER)
                if text:
                    yield {"type": "token", "text": text}  # whatever partial output arrived
                try:
                    info = json.loads(meta)
                except Exception:  # noqa: BLE001
                    info = {}
                yield {"type": "error", "kind": "serve_failed",
                       "message": info.get("message", "host failed to serve"),
                       "spent_msat": spent_msat,                       # what we actually paid
                       "delivered_tokens": info.get("delivered_tokens")}
                return  # host fault: finally records the failed attempt against reputation
            if DONE_MARKER in body:
                text = body.partition(DONE_MARKER)[0]
                if text:
                    yield {"type": "token", "text": text}
                break
            if NEXT_MARKER in body:
                text, _, meta = body.partition(NEXT_MARKER)
                if text:
                    yield {"type": "token", "text": text}
                # live progress: surface sats spent so far so the client isn't dead-air between chunks
                yield {"type": "progress", "spent_msat": spent_msat}
                ch = json.loads(meta)
                continue
            if body:  # no trailer (shouldn't happen) -> stop
                yield {"type": "token", "text": body}
            break

        ok = True
        latency_ms = (time.monotonic() - t0) * 1000
        yield {"type": "done", "spent_msat": spent_msat, "latency_ms": round(latency_ms, 1)}
    except (GeneratorExit, KeyboardInterrupt):
        # User/client CANCELLED mid-stream (GUI Cancel aborts the request -> the server closes this
        # generator; Ctrl-C on the CLI). That's the user's choice, NOT the host's fault — leave its
        # reputation neutral. Closing here also means the pay loop stops: no further chunk is paid.
        penalize = False
        raise
    except httpx.TransportError as e:  # couldn't reach/keep the HOST (Tor/host down) — host-side
        yield {"type": "error", "kind": "unreachable", "message": str(e)}
    except Exception as e:  # noqa: BLE001 - a host-side serve/response failure
        yield {"type": "error", "kind": "other", "message": str(e)}
    finally:
        # Only HOST-fault outcomes reach record(): success, serve_failed, unreachable, bad response.
        # Payment/config failures and user cancels set penalize=False (host untouched). A single
        # host failure won't hide the host (needs N consecutive within the cooldown — see reputation).
        if penalize:
            reputation.record(host.pubkey, success=ok, latency_ms=(latency_ms if ok else None))


def run_inference_bolt11(host, prompt: str, max_tokens: int = 64,
                         poll_seconds: float = 2.0) -> Iterator[dict]:
    """Manual BOLT11 fallback for non-NWC wallets: the host issues ONE invoice for the ceiling,
    the user pays it from any wallet, the host confirms settlement via its own LND, then streams.
    Yields: {"type":"invoice", bolt11, amount_msat, payment_hash}, optional {"type":"waiting"},
    {"type":"token"}, {"type":"done", spent_msat, latency_ms}, or {"type":"error", kind, message}.
    """
    ep = host.endpoint
    proxy = proxy_for(ep)
    read_to = float(os.getenv("CLIENT_READ_TIMEOUT", "300"))
    stream_timeout = httpx.Timeout(connect=15.0, read=read_to, write=15.0, pool=15.0)
    t0 = time.monotonic()
    ok = False
    spent_msat = 0
    latency_ms = 0.0
    try:
        r = httpx.post(f"{ep}/v1/inference/bolt11",
                       json={"prompt": prompt, "max_tokens": max_tokens}, proxy=proxy)
        if r.status_code != 200:
            raise RuntimeError(f"bolt11 create failed: {r.status_code} {r.text[:200]}")
        inv = r.json()
        sid = inv["session_id"]
        yield {"type": "invoice", "bolt11": inv["invoice"], "amount_msat": inv["amount_msat"],
               "payment_hash": inv["payment_hash"], "expires_in": inv.get("expires_in")}

        # Poll until the host's node sees the (foreign-wallet) payment settle, or it expires.
        while True:
            st = httpx.get(f"{ep}/v1/inference/bolt11/{sid}/status", proxy=proxy, timeout=15.0).json()
            state = st.get("state")
            if state == "settled":
                break
            if state == "expired":
                yield {"type": "error", "kind": "expired",
                       "message": "invoice expired before payment"}
                return
            yield {"type": "waiting"}
            time.sleep(poll_seconds)

        # Settled -> stream the response (single payment, up to max_tokens; ceiling already paid).
        with httpx.stream("POST", f"{ep}/v1/inference/bolt11/{sid}/stream",
                          timeout=stream_timeout, proxy=proxy) as s:
            if s.status_code != 200:
                raise RuntimeError(f"stream failed: {s.status_code} {s.read().decode(errors='replace')[:200]}")
            body = "".join(s.iter_text())
        text, _, meta = body.partition(DONE_MARKER)
        if text:
            yield {"type": "token", "text": text}
        spent_msat = json.loads(meta)["spent_msat"] if meta else inv["amount_msat"]
        ok = True
        latency_ms = (time.monotonic() - t0) * 1000
        yield {"type": "done", "spent_msat": spent_msat, "latency_ms": round(latency_ms, 1)}
    except httpx.TransportError as e:
        yield {"type": "error", "kind": "unreachable", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "kind": "other", "message": str(e)}
    finally:
        reputation.record(host.pubkey, success=ok, latency_ms=(latency_ms if ok else None))
