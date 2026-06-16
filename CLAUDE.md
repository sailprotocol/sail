# CLAUDE.md — inference-net

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
- `shared/registry.py`: local dir → Nostr relays *(`REGISTRY=nostr` done; Tor next)*
- `host/moderation.py`: stubs → real CSAM image-hash matching + governance allowlist

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
  - `PAYMENTS` = `mock` | `lnd`
  - `LND_REST_HOST`, `LND_TLS_CERT_PATH`, `LND_MACAROON_PATH` — this process's own LND node
  - `MODEL` = `mock` | `ollama`; `OLLAMA_MODEL`, `OLLAMA_URL`
  - `REGISTRY` = `local` | `nostr`; `NOSTR_RELAYS` (comma-separated), `NOSTR_HOST_NSEC` (host only, never commit)
  - `PORT`, `HOST_ENDPOINT`, `PRICE_MSAT_PER_TOKEN`

## Run & test
- **Smoke test (must always pass):** `PAYMENTS=mock MODEL=mock PYTHONPATH=. .venv/bin/python smoke_test.py`
- Host (real model + regtest LND): `ENV_FILE=.env.host PYTHONPATH=. .venv/bin/uvicorn host.daemon:app --port 8001`
- Client (regtest LND): `ENV_FILE=.env.client PYTHONPATH=. .venv/bin/python -m client.cli "your prompt"`

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
**Phase 1** — replace `MockLightning` with real **LND over regtest** so sats settle through
the L402 handshake. Model seam is already real (Ollama). Next after LND: Nostr discovery.
