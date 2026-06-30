# SAIL — Sovereign AI Inference Layer

**Pay-per-token AI inference over Lightning. Non-custodial, open, censorship-resistant.**

SAIL is a decentralized network where anyone can run an AI model behind a Lightning paywall and
anyone can use it — discover a host, send a prompt, pay per output token, stream the response. No
accounts, no API keys, no middleman holding your funds or your data.

🌐 [sailprotocol.com](https://sailprotocol.com)

---

## Why SAIL

- **Non-custodial.** You pay host invoices straight from your own Lightning wallet. The app never
  holds your funds.
- **Open-core.** The protocol and reference client are open (MIT) and forkable — that's the moat and
  the decentralization. Run your own host or client; nobody can lock you out.
- **Censorship-resistant.** Hosts are discovered over **Nostr** and reachable over **Tor**
  (`.onion`), so there's no central endpoint to block, and a host's IP stays hidden.
- **Pay for what you use.** Metered per output token over **L402**; with an NWC wallet you pay
  per-chunk as it streams.

## Install (Linux beta)

Grab the latest build from [**Releases**](https://github.com/sailprotocol/sail/releases/latest):

- **AppImage** (portable, no install):
  ```bash
  wget https://github.com/sailprotocol/sail/releases/latest/download/SAIL-x86_64.AppImage
  chmod +x SAIL-x86_64.AppImage
  ./SAIL-x86_64.AppImage
  ```
- **Debian/Ubuntu** (`.deb`): download `SAIL_*_amd64.deb` from the release and
  `sudo apt install ./SAIL_*_amd64.deb`.

The app bundles everything (Python runtime, Tor) — you don't need Python or a Lightning node
installed to use it as a client.

## Connect a wallet

Open SAIL, pick a host, type a prompt, hit **Send**, and pay. Two ways to pay:

- **NWC (automatic, recommended).** Paste a [Nostr Wallet Connect](https://nwc.dev) string from a
  supporting wallet (Alby, Zeus, Coinos, Primal…). SAIL pays each chunk automatically as the
  response streams.
- **BOLT11 (any wallet).** No NWC? SAIL shows a single **QR + copyable invoice** for the request —
  pay it from **any** Lightning wallet (Strike, BlueWallet, Phoenix, Cash App…). Once it settles,
  the response streams.

Either way, the payment goes from your wallet directly to the host. SAIL never custodies funds.

## Run a host

Have a GPU? Serve a model and earn sats per token — **no app to download to host.** Hosting is a
script, a browser wizard, and a dashboard:

1. **Download & run the install script.** [`scripts/install-host.sh`](scripts/install-host.sh) sets
   up the prerequisites (GPU driver check, Ollama, Tor), the repo + Python venv, and your model.
   It's meant to be **read first, then run** — not piped blindly into a shell.
2. **Finish in your browser.** The daemon opens a local **setup wizard** (detect → model → pricing
   → payout → seed backup → go-live). Pick **phoenixd** (recommended, self-custodial — the wizard
   installs and provisions it for you), or bring your own **LND** or an **NWC** wallet.
3. **Manage from the dashboard.** A local web dashboard shows earnings/activity and a built-in
   **wallet** — balance, receive/fund, withdraw, close/sweep, and recovery-seed backup. The
   operator surface is **localhost-only and never exposed over Tor**.

Going live installs a **systemd service**, so the host auto-restarts and survives reboot, coming
back on the same `.onion` and pubkey.

📖 **[Run-a-host guide](docs/sail-run-a-host-guide.md)** — step-by-step (fast path + manual).
💸 **[How payments work on your host](docs/sail-payments-explainer.md)** — pay-to-open channels,
fees, your recovery seed, withdraw, and close/sweep.

> **phoenixd hosts — the channel cliff.** A fresh phoenixd node can't *receive* until an inbound
> channel opens, which needs a one-time inbound payment of **~25–35k sat**. Until then the wizard
> and dashboard say *"running, but can't receive yet"* rather than "live to earn". Cross it by
> paying your node a one-time **~25–35k sat** inbound from another wallet — details in the
> [payments explainer](docs/sail-payments-explainer.md).

## Honest notes (please read)

- **The host can see your prompt and response.** Inference runs on the host's hardware in plaintext
  — this is **not** private/confidential inference. Don't send anything you wouldn't want the host
  operator to read. (A TEE-backed private tier is on the roadmap, not here yet.)
- **Beta software.** Expect rough edges. **Linux x86_64 only** for now (macOS/Windows later).
- **Keep amounts small.** It's early; use small sats while the network and clients harden.
- **You are responsible for your wallet and your spend.** Set a sensible max-tokens / wallet budget.

## License

MIT — see [LICENSE](LICENSE). © 2026 SAIL contributors.
