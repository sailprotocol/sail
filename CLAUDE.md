# CLAUDE.md — SAIL (Sovereign AI Inference Layer)

## What this is
A decentralized, censorship-resistant, Lightning-native AI **inference network** plus a
resilient **model-weight distribution** layer. Strategy is **open-core**: the protocol and
reference client are open and forkable (that's the moat and the decentralization); revenue
comes from supply we run ourselves and paid services (verified-host program, premium
client, relays). See `ROADMAP.md` for phases.

## Architecture (six layers)
1. **Discovery/identity** — signed host listings over **Nostr** (local `./registry` is the
   Phase-0 stand-in), reachable over **Tor**, with Proof-of-Work on listings.
2. **Client** — desktop; discovers a host, prompts, pays per token, streams the response.
3. **Payments** — **Lightning, L402**, metered per output token. This is the revenue mechanism.
4. **Verification** — reputation + redundant spot-checks.
5. **Moderation** — see Hard Rules.
6. **Weight distribution** — torrent/IPFS index published over Nostr.

## The four swappable seams (mocked → real)
- `host/payments.py`: `MockLightning` → `LndLightning` (regtest → mainnet)
- `host/model.py`: `MockModel` → `OllamaModel`/vLLM  *(Ollama already wired: `qwen3:14b` on local RTX 3060)*
- `shared/registry.py`: local dir → Nostr relays *(`REGISTRY=nostr` done)*; reach hosts over Tor via `TRANSPORT=tor` (`host/transport.py`)
- `host/moderation.py`: allowlist mechanism + fail-closed CSAM image gate done; real hash-DB matcher (PhotoDNA/NCMEC, needs enrollment) + allowlist governance deferred to Phase 4

## Repo layout
`shared/` (listing, l402, registry) · `host/` (daemon, payments, model, moderation) ·
`client/` (cli) · `smoke_test.py`

## Environment (always use the project venv)
- All Python runs through the project venv. Either activate it (`source .venv/bin/activate`,
  or use `direnv` for auto-activation) or call binaries directly (`.venv/bin/python`,
  `.venv/bin/uvicorn`). Never use the system Python — deps live only in `.venv`.
- The venv is per-shell: each terminal (and the host/client run in separate terminals) needs it.

## Config (.env files)
- Config is loaded with `python-dotenv` from the path in env var `ENV_FILE` (default `.env`).
- Two filled, gitignored files for dev — one per Polar node: the host process uses
  `ENV_FILE=.env.host`, the client uses `ENV_FILE=.env.client`. Template: `.env.example` (committed).
- Standardized variable names:
  - `PAYMENTS` = `mock` | `lnd` | `nwc`
  - `LND_REST_HOST`, `LND_TLS_CERT_PATH`, `LND_MACAROON_PATH` — this process's own LND node (`PAYMENTS=lnd`)
  - `NWC_URI` (client NWC connection string, secret) / `NWC_PATH` (GUI store, gitignored) — `PAYMENTS=nwc`, non-custodial
  - `MODEL` = `mock` | `ollama`; `OLLAMA_MODEL`, `OLLAMA_URL`, `OLLAMA_KEEP_ALIVE` (warm model), `CLIENT_READ_TIMEOUT` (client)
  - `REGISTRY` = `local` | `nostr`; `NOSTR_RELAYS` (comma-separated), `NOSTR_HOST_NSEC` (host only, never commit)
  - `TRANSPORT` = `clearnet` | `tor`; `TOR_CONTROL_PORT`, `ONION_KEY_PATH` (host, key never commit), `TOR_SOCKS` (client)
  - `POW_TARGET` (host, NIP-13 listing PoW bits), `POW_MIN_DIFFICULTY` (client rejects below), `REPUTATION_PATH` (client, gitignored)
  - `MODEL_ALLOWLIST` / `MODEL_ALLOWLIST_PATH` (host refuses to serve + client filters; unset = permissive), `CSAM_HASHER` (image gate; unset = image disabled, fail-closed)
  - `REPUTATION_PATH`, `HISTORY_PATH` (client GUI local stores; both gitignored)
  - `PORT`, `HOST_ENDPOINT`, `PRICE_MSAT_PER_TOKEN`, `CHUNK_TOKENS` (metered chunk size), `BOLT11_EXPIRY_SECONDS` (manual-pay window)

## Run & test
- **Smoke test (must always pass):** `PAYMENTS=mock MODEL=mock PYTHONPATH=. .venv/bin/python smoke_test.py`
- Host (real model + regtest LND): `ENV_FILE=.env.host PYTHONPATH=. .venv/bin/uvicorn host.daemon:app --port 8001`
- Client CLI (regtest LND): `ENV_FILE=.env.client PYTHONPATH=. .venv/bin/python -m client.cli "your prompt"`
- Client GUI (local web app): `ENV_FILE=.env.client PYTHONPATH=. .venv/bin/uvicorn client.webapp:app --port 8080` → open `http://127.0.0.1:8080`. CLI + GUI share `client/core.py`. (Tauri/desktop packaging deferred to a later task.)

## Conventions
- Keep backends behind their interfaces (`LightningBackend`, `ModelBackend`); select via env vars.
- All config via env / `.env*`. Never hardcode hosts, ports, or keys.
- Idiomatic, typed Python. Small, focused, reviewable changes — one seam at a time.

## Hard rules (do not violate)
- **Always keep the mock path working.** After every change, `PAYMENTS=mock MODEL=mock python smoke_test.py` must still pass.
- **Never commit secrets** — LND macaroons, `tls.cert`, `.env`, wallet seeds. They stay gitignored. Read credentials from env.
- **Lightning uses a DEDICATED node** (regtest now). Never touch, reference, or connect to the AUPA BTCPay node.
- **Moderation seams are non-negotiable.** Never weaken, bypass, or stub out the CSAM image-hash check or the harm-model allowlist to "simplify." Image models do not ship until the CSAM hash check is real.
- **Keep the core open.** No closed or proprietary hard dependencies inside the protocol/client.

## Current phase
**Phase 2** — Phase 1's core code is **done & verified**: LND/L402 payments, Ollama
inference, Nostr discovery, and Tor `.onion` transport all run end-to-end (mock/local paths
still selectable via env). Verified cross-network (Starlink → home host over a public relay +
Tor). Now building toward a **beta**: metered settlement (close the prepay gap), reputation +
spot-check verification, the moderation seam (CSAM image-hash + allowlist), a real GUI client,
and standing up real infra/hosts. Leftover Phase-1 ops items (mainnet LND, more hosts) carry
in alongside.
