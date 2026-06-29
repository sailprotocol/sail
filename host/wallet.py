"""
Host wallet (phoenixd) — see and move the operator's sats from the dashboard, no terminal needed.

phoenixd is the self-custodial Lightning node behind the recommended payout tier. It uses
pay-to-open AUTOMATIC channels: there is NO manual "open channel" command. A channel opens by
itself once a sufficient inbound payment (~25-35k+ sat) arrives. So the wallet UI frames "open a
channel" as "show a receive invoice; paying it auto-opens your channel" — not as channel config.

This wraps phoenixd's local HTTP API, reusing the SAME node + config the payment flow uses
(PHOENIXD_API_URL + PHOENIXD_API_PASSWORD). It is phoenixd-specific: the daemon exposes the wallet
endpoints only when PAYMENTS=phoenixd, and only behind the local-only gate (never over the onion).

Stage 1 here is read-only (balance + channels). Receive/pay/close land in later stages.
"""
from __future__ import annotations

import os

import httpx


class WalletError(RuntimeError):
    """A phoenixd call failed. The message is operator-facing (local dashboard only)."""


def _channel_state(c: dict) -> str:
    """phoenixd reports channel state in 'type' as a fully-qualified lightning-kmp class name, e.g.
    'fr.acinq.lightning.channel.states.Normal' (no bare 'state' field). Take the last dotted
    component; fall back to a legacy 'state' field if a future build adds one."""
    raw = c.get("type") or c.get("state") or ""
    return str(raw).rsplit(".", 1)[-1] or "unknown"


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class PhoenixdWallet:
    """Thin wrapper over phoenixd's HTTP API. Pass a client in tests; defaults to env config."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or _default_client()

    # --- read-only -----------------------------------------------------------
    def balance(self) -> dict:
        """Spendable Lightning balance. phoenixd: {balanceSat, feeCreditSat} — feeCreditSat is held
        until a channel opens and is NOT yet spendable, so we surface it separately."""
        j = self._get("/getbalance")
        return {"balanceSat": _int(j.get("balanceSat")), "feeCreditSat": _int(j.get("feeCreditSat"))}

    def channels(self) -> dict:
        """Channels with per-channel state + inbound/outbound liquidity, plus derived totals and a
        canReceive flag (true when there's any inbound capacity to be paid into)."""
        raw = self._get("/listchannels")
        rows = raw if isinstance(raw, list) else []
        out, inbound_total, outbound_total = [], 0, 0
        for c in rows:
            if not isinstance(c, dict):
                continue
            inbound = _int(c.get("inboundLiquiditySat"))
            outbound = _int(c.get("balanceSat"))  # phoenixd: local balance = what you can send
            inbound_total += inbound
            outbound_total += outbound
            out.append({"channelId": c.get("channelId"), "state": _channel_state(c),
                        "inboundSat": inbound, "outboundSat": outbound})
        return {"channels": out, "count": len(out), "inboundSat": inbound_total,
                "outboundSat": outbound_total, "canReceive": inbound_total > 0}

    # --- receive (fund / first-payment to auto-open the channel) -------------
    def receive(self, amount_sat: int | None = None, description: str | None = None) -> dict:
        """Mint a BOLT11 the operator (or their first customer) can pay. A first payment of
        ~25-35k+ sat auto-opens the channel (pay-to-open) — phoenixd has no manual open. amountSat
        optional (blank = any-amount invoice). Returns {bolt11, paymentHash, amountSat, description}."""
        desc = (description or "SAIL host wallet").strip()
        data = {"description": desc}
        if amount_sat is not None:
            data["amountSat"] = str(int(amount_sat))
        j = self._post("/createinvoice", data)
        return {"bolt11": j.get("serialized"), "paymentHash": j.get("paymentHash"),
                "amountSat": amount_sat, "description": desc}

    # --- withdraw (pay an external invoice) ----------------------------------
    def pay(self, invoice: str, amount_sat: int | None = None) -> dict:
        """Pay an external BOLT11 (withdraw via Lightning). phoenixd /payinvoice. amountSat only for
        an any-amount invoice. Returns what actually went out + the routing fee."""
        inv = (invoice or "").strip()
        if not inv:
            raise WalletError("no invoice provided")
        data = {"invoice": inv}
        if amount_sat is not None:
            data["amountSat"] = str(int(amount_sat))
        j = self._post("/payinvoice", data)
        return {"paymentHash": j.get("paymentHash"),
                "recipientAmountSat": _int(j.get("recipientAmountSat")),
                "routingFeeSat": _int(j.get("routingFeeSat"))}

    # --- close + sweep on-chain ----------------------------------------------
    def close(self, channel_id: str, address: str, feerate_sat_byte: int) -> dict:
        """Close one channel, sweeping its on-chain remainder to `address`. phoenixd /closechannel
        returns the closing txid (plain text or JSON depending on build) — tolerate both."""
        data = {"channelId": channel_id, "address": address,
                "feerateSatByte": str(int(feerate_sat_byte))}
        r = self._ok(self._call("POST", "/closechannel", data))
        body = (getattr(r, "text", "") or "").strip()
        try:
            j = r.json()
            txid = (j.get("txId") or j.get("txid")) if isinstance(j, dict) else (j or body)
        except ValueError:
            txid = body
        return {"channelId": channel_id, "closingTxId": txid or body}

    # --- payment-landed check (for the receive modal's confirmation) ---------
    def incoming_status(self, payment_hash: str) -> dict:
        """Has a specific incoming invoice been paid? phoenixd /payments/incoming/{hash}. A 404 means
        not seen yet (treat as unpaid) so polling is safe before the payment lands."""
        r = self._call("GET", f"/payments/incoming/{payment_hash}")
        if r.status_code == 404:
            return {"paid": False, "receivedSat": 0}
        j = self._json(self._ok(r), "incoming")
        return {"paid": bool(j.get("isPaid")), "receivedSat": _int(j.get("receivedSat"))}

    # --- internals -----------------------------------------------------------
    def _call(self, method: str, path: str, data: dict | None = None):
        try:
            return self._client.get(path) if method == "GET" else self._client.post(path, data=data)
        except httpx.HTTPError as e:
            raise WalletError(f"phoenixd unreachable at {self._client.base_url}: {str(e)[:120]}") from e

    @staticmethod
    def _ok(r):
        if r.status_code != 200:
            body = (getattr(r, "text", "") or "").strip()
            raise WalletError(f"phoenixd HTTP {r.status_code}" + (f": {body[:160]}" if body else ""))
        return r

    def _get(self, path: str):
        return self._json(self._ok(self._call("GET", path)), path)

    def _post(self, path: str, data: dict):
        return self._json(self._ok(self._call("POST", path, data)), path)

    @staticmethod
    def _json(r, path: str):
        try:
            return r.json()
        except ValueError as e:
            raise WalletError(f"phoenixd returned non-JSON from {path}") from e


def _default_client() -> httpx.Client:
    base = os.getenv("PHOENIXD_API_URL", "http://127.0.0.1:9740").rstrip("/")
    password = os.getenv("PHOENIXD_API_PASSWORD", "")
    # Basic auth: empty username + the http-password (phoenixd's scheme), same as the payment flow.
    return httpx.Client(base_url=base, auth=("", password), timeout=15.0)


def get_wallet() -> PhoenixdWallet:
    return PhoenixdWallet()
