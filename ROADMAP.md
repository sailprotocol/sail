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

## Phase 1 — Real rails, your own hosts  *(4–8 weeks — this is Milestone 1 from the spec)*
**Goal:** sats actually flow to *you* from a real model over real Lightning.

**Ships**
- Swap `MockLightning` → **LND** (regtest first, then mainnet) with real L402 (Aperture or custom against LND gRPC). Dedicated node — **not** AUPA's.
- Swap `MockModel` → **vLLM** (datacenter cards) or **Ollama/llama.cpp** (consumer cards) serving a real open model.
- Swap local registry → **real Nostr** relay publish/discover; reach hosts over **Tor**.
- CSAM image-output hash-check hook live on any image models.
- You run 1–3 of your own GPU hosts.

**Exit criteria:** a prompt from the client runs on your real host, settles real (or regtest) sats per token, and the listing is discoverable over Nostr.

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

## Sequencing notes
- Phases 0→1 are strictly sequential. 2, 3, 4 overlap.
- Don't build verification beyond reputation, the token model, or mobile until the paid loop has real users.
- Keep entity/IP/time cleanly separated from any employer (e.g. a future SpaceX role) from Phase 1 onward.
