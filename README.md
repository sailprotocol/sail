# inference-net — Phase 0 scaffold

A working **proof-of-loop** for the decentralized inference network: a client discovers a
host, pays per output token over an **L402** Lightning handshake, and streams the response
while metering sats. Every hard integration (Lightning, model, discovery) sits behind a
clean interface that is mocked in Phase 0 and swapped for the real thing in Phase 1.

See `ROADMAP.md` for the phases and the v1 spec for the open-core revenue strategy.

## What works right now

`discover → 402 challenge → pay → L402 retry → metered token stream`, end to end, with
real preimage/hash crypto on the payment handshake. No GPU, no Lightning node, no network.

```
pip install -r requirements.txt
PYTHONPATH=. python3 smoke_test.py        # in-process, validates the whole loop
```

Or run it as two real processes:

```
# terminal 1 — host
PYTHONPATH=. PAYMENTS=mock MODEL=mock PORT=8001 uvicorn host.daemon:app --port 8001

# terminal 2 — client
PYTHONPATH=. python3 -m client.cli "Explain Lightning in one sentence"
```

## Layout

```
shared/
  listing.py     # Nostr host-listing schema (real shape; local transport for now)
  l402.py        # L402 challenge / parse / verify (real sha256 preimage check)
  registry.py    # discovery: local ./registry dir now -> Nostr relays in Phase 1
host/
  daemon.py      # FastAPI: L402-gated, metered, streaming inference + listing publish
  payments.py    # LightningBackend: MockLightning (works) | LndLightning (Phase 1 stub)
  model.py       # ModelBackend:   MockModel (works) | OllamaModel (Phase 1)
  moderation.py  # CSAM image-hash hook + allowlist seam (the hard line)
client/
  cli.py         # discover, pay (mock reveal), stream, tally sats
smoke_test.py    # in-process end-to-end check
```

## Mocked now → real in Phase 1 (the seams)

| Interface | Phase 0 | Phase 1 |
|---|---|---|
| `host/payments.py` | `MockLightning` reveals preimage locally | `LndLightning` against a **dedicated** LND node (regtest→mainnet) + real L402 |
| `host/model.py` | `MockModel` echoes tokens | `OllamaModel` / vLLM serving a real open model |
| `shared/registry.py` | local `./registry/*.json` | Nostr relay publish/subscribe over Tor |
| `host/moderation.py` | no-op stubs | perceptual-hash CSAM matching on image outputs; governance allowlist |
| payment per request | one macaroon per request | streaming/renewing macaroons, spend caps in caveats |

## Important notes

- **`/mock/pay` is Phase-0 only.** It simulates the LN network revealing the preimage to
  the payer. It is disabled when `PAYMENTS=lnd`; a real payer learns the preimage by paying.
- **Use a dedicated Lightning node for Phase 1 — not the AUPA BTCPay node.** Keep this
  venture's funds, keys, and entity fully separate.
- Listings are replaceable by pubkey; the demo host uses a random pubkey per run, so
  `rm -rf registry` between runs if stale entries accumulate.
- Pricing in the demo is 1 sat/token for legibility; real pricing is set per host listing.
