# Roadmap — Decentralized Inference Network

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
- Host onboarding so external GPUs can join.
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

---

## Sequencing notes
- Phases 0→1 are strictly sequential. 2, 3, 4 overlap.
- Don't build verification beyond reputation, the token model, or mobile until the paid loop has real users.
- Keep entity/IP/time cleanly separated from any employer (e.g. a future SpaceX role) from Phase 1 onward.
