# Roadmap — SAIL (Sovereign AI Inference Layer)

Open-core strategy: the **protocol + reference client stay open** (decentralization + moat); **revenue comes from supply you run + paid services** (verified-host program, premium client, relays). See the v1 spec for the full rationale.

Guiding rule: **get sats flowing through the smallest possible loop before scaling or polishing anything.**

---

## Phase 0 — Proof-of-loop  *(this scaffold — days, not weeks)*
**Goal:** see the full L402 + token-streaming + metering loop work end-to-end on one machine, with every hard integration mocked behind a clean interface.

**Ships**
- One host daemon (FastAPI) exposing an L402-gated inference endpoint.
- One client (CLI) that discovers a host, completes the L402 handshake, pays (mock), and streams tokens while tallying sats.
- Mock Lightning, mock/Ollama model backend, local-file "registry" standing in for Nostr.

**Exit criteria:** `client` prints a streamed response and a sats-spent total after a mock payment. The seams for real LN / real model / real Nostr are obvious and isolated.

---

## Phase 1 — Real rails, your own hosts  *(this is Milestone 1 from the spec)*
**Goal:** sats actually flow to *you* from a real model over real Lightning.

**Ships**
- ✅ Swap `MockLightning` → **LND** with real L402 (REST, via httpx). Dedicated node — **not** AUPA's. *(Done: regtest payment verified end-to-end — dave-host invoice SETTLED `amt_paid_msat=64000`, alice-client payment SUCCEEDED, 0 fee.)*
- ✅ Swap `MockModel` → **Ollama** serving `qwen3:14b` on the RTX 3060.
- ✅ Swap local registry → **real Nostr** relay publish/discover (`REGISTRY=nostr`: signed kind-38111 listings, client verifies signatures, dedupes by pubkey).
- ✅ Reach hosts over **Tor** (`TRANSPORT=tor`: host exposes a v3 `.onion`, advertises it in the listing, persists the onion key; client routes `.onion` endpoints over Tor's SOCKS proxy). *(Verified cross-network: client on **Starlink** reached the home host through a **public Nostr relay + Tor** — host IP hidden, no port-forwarding/NAT traversal.)*
- ☐ CSAM image-output hash-check hook live on any image models.
- ☐ Mainnet: point `LND_*` env vars at a real node (no code change).
- ☐ Run 1–3 of your own GPU hosts.

**Status:** Phase 1's **core code is complete and verified** — LND/L402 payments, Ollama inference, Nostr discovery, and Tor transport all run end-to-end (mock/local paths preserved behind env switches). Remaining Phase 1 items are operational, not core-protocol: **mainnet LND** (config-only), the **CSAM image-hash hook** (gates image models), and **running more hosts**.

**Exit criteria:** ✅ met for the text path — a prompt from the client runs on the real host, settles regtest sats per token, and the listing is discoverable over Nostr (and reachable over Tor from any network).

> **✅ Metered settlement (prepay gap) — done (Phase 2).** Was: invoice the full
> `max_tokens × price` upfront and settle it regardless of tokens delivered (64 sat for 28
> tokens). Now: **pay-as-you-go chunks** — the host sells tokens in `CHUNK_TOKENS`-sized
> chunks, each its own L402 payment, and only invoices the next chunk if more output remains
> (1-token look-ahead). The client pays for tokens actually delivered; overpay is bounded to
> **< 1 chunk** (zero with `CHUNK_TOKENS=1`). No client identity, credit state, or refunds.

---

## Phase 2 — Open the network + reputation  *(2–3 months)*
**Goal:** third parties can host and use it; trust is decentralized.

**Ships**
- Polished reference client (begin migration to **Tauri** per spec).
- **Host onboarding so external GPUs can join** — the "wallet + GPU and go" track:
  - ✅ Durable host as a `sail-host` **systemd service** (auto-restart, survives reboot, same onion/pubkey).
  - ✅ Docs: systemd documented as the **canonical** host setup (README + wiki); manual `uvicorn` demoted to dev-only.
  - **Tiered payouts** — `phoenixd` default (self-custodial, auto-liquidity), own **LND** (most sovereign), **NWC** (bring-your-own, labeled). Keeps the easy tier self-custodial so "easy" doesn't fight "sovereign."
  - **One app, two views** (Use / Host); the host runs as a background daemon regardless of which view is open.
  - **First-run wizard**: detect GPU → pick model → set pricing → choose payout rail (→ seed backup for phoenixd) → go live.
  - **Host controls**: pause / resume / stop & remove.
  - **Pubkey-derived two-word aliases** (e.g. `eloquent-cat`) — friendly, unforgeable host identity with no registrar; client recomputes + verifies from the listing pubkey.
  - **Simultaneous host + client** supported — separate identity per role; self-connection guard.
- Reputation + bond; **Proof-of-Work on listings** (anti-spam).
- Redundant spot-check verification (silently re-run a fraction of prompts, compare).
- Manual verified-host program begins.

**Exit criteria:** first external host and first external user complete a paid inference without your hand-holding.

---

## Phase 3 — Monetize the services layer  *(ongoing, starts alongside Phase 2)*
**Goal:** durable, fork-proof revenue.

**Ships**
- **Verified-host certification fees** + **premium client** (memory, agents, nicer UX) + **high-reliability relay/gateway**.
- **Soft-enforced protocol fee** (~1–2%), reputation-gated.
- **Separate BTCPay instance** for the invoice-shaped billing: subscriptions, session top-ups, premium tiers. (Core per-token payments stay on the direct LN/L402 path.)

**Exit criteria:** first recurring revenue that isn't your own-supply inference income.

---

## Phase 4 — Resilience & decentralization hardening  *(ongoing)*
**Goal:** the unkillable properties and the mission flagship.

**Ships**
- **Weight-distribution layer** — torrent/IPFS index of model weights published over Nostr (keeps banned weights alive; your least-crowded differentiator).
- I2P / Nym transports alongside Tor.
- Bonded mediator/arbitration for takedowns and disputes.

---

## Phase 5 — Future / optional  *(only with traction + counsel)*
- TEE-backed **private inference tier** on capable datacenter hardware (don't promise host-blind privacy on consumer GPUs).
- DAO / token model to fund contributors — **only after a securities-law conversation**.
- Mobile / sideload (Aurora) client.

---

## GUI / client backlog
Follow-ups for the web client (`client/webapp.py` + `client/static/`, shared logic in
`client/core.py`). Not blocking; pick up alongside Phase 2/3.
- **Host-unreachable UX.** When a host fails to connect/stream, show a friendly
  "host unreachable — trying another" state instead of a raw errno, and auto-skip /
  deprioritize dead hosts. Lean on the existing reputation drop logic (`client/reputation.py`):
  a failure is already recorded, so re-rank and fail over to the next-best host automatically.
- **Session history.** Persist past inference sessions locally (prompt, response, host pubkey,
  sats spent, timestamp); let the GUI list previous sessions and reopen them. Gitignored, like
  the reputation store.
- **Payment transparency & pre-pay confirmation.** *(trust-critical beta-hardening)* Before
  paying, show the host + price/token + **max cost** (`price × max_tokens`) with an explicit
  confirm step — no silent spend. During streaming, show **running sats spent live** as chunks
  settle. Replace the bare "paying…" state with **"paying N sats to `<model>`"** so the user
  always knows who they're paying and how much.
- **✅ Broad wallet support / BOLT11 fallback — landed.** Non-NWC wallets (Strike, BlueWallet,
  Phoenix, Cash App) pay a single ceiling invoice (`price × max_tokens`) shown as **QR + copy**;
  the host confirms settlement via its own LND, then streams. NWC stays the fine-grained automated
  path. **Known v1 gap:** a manual payment that settles but whose inference then fails is **not
  refunded** (user bounds spend via `max_tokens`); needs settlement-aware refund/credit later.
- **Latency robustness.** Per-token streaming so the user sees progress mid-chunk (not just at
  chunk boundaries); tune `CHUNK_TOKENS` vs. payment round-trips; show a clear "still
  generating…" state instead of a silent wait/timeout. (Server keeps the model warm via
  `OLLAMA_KEEP_ALIVE`; the client read timeout resets per chunk and is tunable.)

---

## Wallet v0.3 polish (deferred — host wallet UI is shipping in v0.2)
The host wallet (balance/channels, receive+QR, seed backup, withdraw, close/sweep, seed import)
ships in v0.2. These are once-per-setup niceties, not blockers — defer:
- **Per-word seed entry.** Replace the import textarea with 12 individual word boxes that validate
  per-word (turn green on a valid BIP39 word), with the whole field turning green once the full
  phrase passes the BIP39 checksum.
- **Word autocomplete.** Dropdown suggestions from the 2048-word BIP39 list (`shared/bip39.py`) as
  the operator types each word, to prevent typos.

## Sequencing notes
- Phases 0→1 are strictly sequential. 2, 3, 4 overlap.
- Don't build verification beyond reputation, the token model, or mobile until the paid loop has real users.
- Keep entity/IP/time cleanly separated from any employer (e.g. a future SpaceX role) from Phase 1 onward.
