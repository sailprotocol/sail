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


# A cooperative channel close is roughly 1 input + ~2 outputs; ~200 vB is a safe over-estimate.
# phoenixd has no close-fee API, so we estimate feerate × this vsize and LABEL it an estimate.
CLOSE_TX_VSIZE = 200
DUST_LIMIT_SAT = 546  # standard P2WPKH dust; below this a sweep output is "little or nothing"


def suggested_feerate() -> int:
    """A starting feerate (sat/vByte) to PREFILL the close form so the operator isn't guessing from
    nothing. phoenixd exposes no feerate API and we avoid external services (sovereignty), so this is
    a sane, editable default (override with CLOSE_FEERATE_DEFAULT). A cooperative close isn't urgent,
    so a modest default is fine; the operator should still sanity-check against a mempool estimator."""
    try:
        v = int(os.getenv("CLOSE_FEERATE_DEFAULT", "2"))
    except (TypeError, ValueError):
        v = 2
    return max(1, v)


def estimate_close_quote(balance_sat: int, feerate_sat_byte: int) -> dict:
    """Estimate what a channel close pays out: the on-chain fee and the NET the operator receives at
    `feerate`, plus a dust flag so they don't close blind and get nothing. Pure (no phoenixd)."""
    balance_sat = _int(balance_sat)
    feerate_sat_byte = int(feerate_sat_byte)
    fee = feerate_sat_byte * CLOSE_TX_VSIZE
    net = balance_sat - fee
    dust = net <= DUST_LIMIT_SAT
    if balance_sat <= fee:
        detail = (f"your channel balance (~{balance_sat} sat) is at or below the on-chain close fee "
                  f"(~{fee} sat) — you'd receive little or nothing. Consider withdrawing over "
                  f"Lightning (Withdraw) instead.")
    elif dust:
        detail = (f"after the ~{fee} sat close fee you'd receive only ~{max(net, 0)} sat (near the "
                  f"dust limit). Consider withdrawing over Lightning (Withdraw) instead.")
    else:
        detail = (f"estimated on-chain fee ~{fee} sat at {feerate_sat_byte} sat/vB; "
                  f"you'd receive ~{net} sat.")
    return {"balanceSat": balance_sat, "feerateSatByte": feerate_sat_byte,
            "estVsize": CLOSE_TX_VSIZE, "feeSat": fee, "netSat": max(net, 0),
            "dust": dust, "estimate": True, "detail": detail}


# lightning-kmp channel states that mean "closing / settling on-chain" (not open, not fully Closed).
# funds are moving on-chain and are NOT spendable over Lightning while in these states.
_CLOSING_STATES = {"Negotiating", "ShuttingDown", "Closing"}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class PhoenixdWallet:
    """Thin wrapper over phoenixd's HTTP API. Pass a client in tests; defaults to env config."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        # Lazy: don't build the client (which resolves the phoenix.conf password) until an actual API
        # call is made. So constructing a wallet is cheap and side-effect-free — input validation can
        # run first, and the password is resolved fresh per request (reinforcing no .env.host drift).
        self._client = client

    def _conn(self) -> httpx.Client:
        if self._client is None:
            self._client = _default_client()
        return self._client

    # --- read-only -----------------------------------------------------------
    def balance(self) -> dict:
        """Spendable Lightning balance. phoenixd: {balanceSat, feeCreditSat} — feeCreditSat is held
        until a channel opens and is NOT yet spendable, so we surface it separately."""
        j = self._get("/getbalance")
        return {"balanceSat": _int(j.get("balanceSat")), "feeCreditSat": _int(j.get("feeCreditSat"))}

    def channels(self) -> dict:
        """Channels with per-channel state + inbound/outbound liquidity, derived totals, and flags.

        Read from /getinfo, NOT /listchannels: phoenixd's /listchannels returns the raw lightning-kmp
        channel objects (no top-level balance fields — they read as 0). /getinfo returns the clean
        per-channel view (ApiType.Channel): `state` (short, e.g. "Normal"), `balanceSat` =
        availableBalanceForSend (OUTBOUND / can send — matches /getbalance), `inboundLiquiditySat` =
        availableBalanceForReceive (INBOUND / can receive), `capacitySat`.

        Only OPEN ("Normal") channels are live: a Closed channel's liquidity is gone on-chain, and a
        channel mid-close (Negotiating/ShuttingDown/Closing) is settling — neither is spendable. So
        inbound/outbound + the channel count reflect ONLY open channels (else a fully-closed wallet
        shows phantom "can send" while getbalance is 0). Closing channels are reported separately so
        the UI can say "closing — funds settling on-chain". `channels` holds only the open (closeable)
        ones. hasChannel = at least one open channel — gate the channel-cliff message on this."""
        info = self._get("/getinfo")
        rows = info.get("channels") if isinstance(info, dict) else None
        rows = rows if isinstance(rows, list) else []
        out, inbound_total, outbound_total, closing = [], 0, 0, 0
        for c in rows:
            if not isinstance(c, dict):
                continue
            state = str(c.get("state") or "unknown")
            if state == "Normal":  # open + usable — the only channels with live liquidity
                inbound = _int(c.get("inboundLiquiditySat"))
                outbound = _int(c.get("balanceSat"))
                inbound_total += inbound
                outbound_total += outbound
                out.append({"channelId": c.get("channelId"), "state": state,
                            "inboundSat": inbound, "outboundSat": outbound,
                            "capacitySat": _int(c.get("capacitySat"))})
            elif state in _CLOSING_STATES:  # cooperative/force close in flight — settling on-chain
                closing += 1
            # Closed / transient (Offline, Syncing, opening…) contribute nothing to live liquidity.
        return {"channels": out, "count": len(out), "openCount": len(out),
                "normalCount": len(out), "closingCount": closing,
                "inboundSat": inbound_total, "outboundSat": outbound_total,
                "hasChannel": len(out) > 0, "canReceive": inbound_total > 0}

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
        c = self._conn()
        try:
            return c.get(path) if method == "GET" else c.post(path, data=data)
        except httpx.HTTPError as e:
            raise WalletError(f"phoenixd unreachable at {c.base_url}: {str(e)[:120]}") from e

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
    from host import phoenixd_setup  # local import — avoids an import cycle
    base = os.getenv("PHOENIXD_API_URL", "http://127.0.0.1:9740").rstrip("/")
    # Read the password live from phoenix.conf (resolve_api_password), so the wallet card always
    # authenticates with the ACTIVE wallet's password — no .env.host drift after an import/restore
    # (the HTTP 401-with-funds bug). Env var is only a remote-phoenixd fallback.
    try:
        password = phoenixd_setup.resolve_api_password()
    except RuntimeError as e:
        raise WalletError(str(e)) from e
    # Basic auth: empty username + the http-password (phoenixd's scheme), same as the payment flow.
    return httpx.Client(base_url=base, auth=("", password), timeout=15.0)


def get_wallet() -> PhoenixdWallet:
    return PhoenixdWallet()
