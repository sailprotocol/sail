# Run a SAIL host

Serve an open model behind a Lightning paywall and earn sats per token. A host runs the SAIL
inference daemon behind your own Lightning backend and a Tor onion service, discovered over
Nostr. This guide takes you from a fresh Ubuntu box to a live, earning host.

> **Set expectations first.** Inference is served over Tor, so responses are **slow by
> design** (a short answer can take ~25–125s depending on model size). That latency is the
> privacy/sovereignty trade — your IP stays hidden and there's no central endpoint. It is not
> a bug. If you want speed over privacy, this isn't the right tool.

---

## What you'll need

- **Linux x86_64** (Ubuntu 22.04/24.04 tested). macOS/Windows not yet supported for hosting.
- **An NVIDIA GPU.** VRAM is the gate on which models you can serve:
  - ~4 GB → small models (e.g. `llama3.2:3b`)
  - 12–16 GB → mid models (e.g. `qwen3:14b`)
  - More VRAM → larger models. You can't serve a model that doesn't fit your VRAM.
- **A Lightning backend to receive payments** — pick one:
  - **phoenixd** (recommended, easiest): self-custodial, auto-liquidity; the wizard installs
    and provisions it for you.
  - **LND** (most sovereign): your own node, connected over REST.
  - **NWC**: receive into a Nostr-Wallet-Connect-capable wallet.
- **~25–35k sat of inbound** if you use phoenixd — see [the channel cliff](#the-phoenixd-channel-cliff) below.

---

## 1. System prerequisites

Install the GPU driver, Ollama (model server), Tor, and Python.

```bash
# NVIDIA driver (if not already installed) — reboot after:
sudo ubuntu-drivers autoinstall && sudo reboot     # then verify:
nvidia-smi                                          # should list your GPU

# Ollama (serves the model locally):
curl -fsSL https://ollama.com/install.sh | sh
ollama --version

# Tor + Python tooling:
sudo apt update && sudo apt install -y tor python3-venv python3-pip git
```

### Enable Tor's control port (required)

The host creates its `.onion` service through Tor's control port. Enable it with cookie auth:

```bash
sudo tee -a /etc/tor/torrc >/dev/null <<'EOF'
ControlPort 9051
CookieAuthentication 1
CookieAuthFileGroupReadable 1
EOF
sudo systemctl restart tor
# Let the daemon's user read the auth cookie:
sudo usermod -aG debian-tor "$USER"     # log out/in (or reboot) for the group to take effect
```

---

## 2. Get the code

```bash
git clone https://github.com/sailprotocol/sail.git
cd sail
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 3. Create your host config

Copy the template. You do **not** need to fill in a payment backend by hand — a fresh host
defaults to `PAYMENTS=mock` and boots straight into the setup wizard, which writes the real
backend for you.

```bash
cp .env.example .env.host
```

Open `.env.host` and set the basics (leave `PAYMENTS=mock` for now — the wizard changes it):

- `MODEL=ollama`, `OLLAMA_MODEL=<a model that fits your VRAM>` (e.g. `llama3.2:3b`)
- `PORT=8001`
- `TRANSPORT=tor`
- `REGISTRY=nostr` and `NOSTR_RELAYS=wss://relay.damus.io,wss://nos.lol`

Everything else (identity, onion key, payout) is created for you on first run.

> **Note:** the template's LND example paths point at a local dev setup — ignore them unless
> you're using your own LND, in which case the wizard's payout step collects the real values.

---

## 4. Start the daemon → it opens the setup wizard

```bash
ENV_FILE=.env.host PYTHONPATH=. .venv/bin/uvicorn host.daemon:app --port 8001
```

A fresh host (mock payments) boots into the wizard. Open it **locally** (the wizard is
local-only and never exposed over the `.onion`):

**→ http://localhost:8001/setup**

The wizard walks you through:

1. **Detect** — checks GPU, Ollama, Tor.
2. **Model** — pick/pull the model you want to serve (streams the pull progress).
3. **Pricing** — sats per token and chunk size (defaults are sane; smaller chunk = tighter
   metering, larger = fewer round-trips over Tor).
4. **Payout** — choose your backend. Pick **phoenixd** and the wizard downloads, first-runs,
   and configures it, writing the API password into `.env.host`. (You'll be shown a 12-word
   seed **once** — write it down; it's your node's recovery.)
5. **Go live** — renders and installs the systemd service so the host auto-restarts and
   survives reboot on the same `.onion` and pubkey.

---

## 5. Your host identity — back it up

On first run the host generates a persistent Nostr identity (your host's pubkey/`.onion` are
derived from it) and saves the secret key at:

```
~/.config/inference-net/host.nsec      # mode 0600 — this is your host's identity
```

**Back this file up.** If you lose it, your host comes back as a *different* host (new pubkey,
new listing) and loses its reputation/history. Treat it like a private key — never commit or
share it. The onion key is likewise persisted (path set by `ONION_KEY_PATH`) so your `.onion`
stays stable across restarts.

---

## 6. Verify you're live

After "go live," the host runs as the `sail-host` systemd service.

```bash
systemctl is-active sail-host                 # → active
# wait for active, THEN check status (querying too early returns empty):
curl -s http://127.0.0.1:8001/api/status | python3 -m json.tool
```

In the status JSON, check:

- `payments_ready: true` — your backend is reachable.
- `receivable: true` — you can actually be paid. **If you're on phoenixd and this is
  `false`,** you're at the channel cliff (next section), not live to earn yet.

Confirm your listing reached the relays — the daemon logs it on start:

```bash
journalctl -u sail-host -n 50 --no-pager | grep -E 'published|accepted|onion'
# expect: published listing ... accepted by N relay(s) ...
```

A client running `--list` should now see your host alias and pubkey.

---

## The phoenixd channel cliff

A fresh phoenixd node **cannot receive** until it has an inbound channel — and it won't open
one until an initial inbound payment exceeds the channel-open cost (**~25–35k sat** at current
fees). Below that, an incoming payment is held as `feeCreditSat` with **no channel**, and your
host **cannot be paid**. The wizard and dashboard will show *"running, but can't receive yet"*
rather than "live to earn," and `/api/status` reports `receivable: false`.

**To cross the cliff:** send your phoenixd node a one-time **~25–35k sat** inbound payment from
another wallet (the wizard/dashboard shows an invoice). That opens the channel; after it
confirms, `receivable` flips to `true` and per-token micro-payments (e.g. 8 sat each) flow
normally. Per-token amounts alone will never bootstrap the channel — you must do the one-time
inbound first.

(LND and NWC backends with existing channels/liquidity don't have this cliff.)

---

## Operating notes

- **Restart after a config change:** `sudo systemctl restart sail-host` (then wait for
  `systemctl is-active sail-host` before hitting `/api/status`).
- **The host sees prompts and responses in plaintext** — this is not confidential inference.
  Operate accordingly.
- **Updating:** `git pull` then `sudo systemctl restart sail-host`.
- A condensed command reference lives in
  [`docs/sail-operator-cheatsheet.md`](./sail-operator-cheatsheet.md).

---

## Troubleshooting

- **Wizard won't create the onion / `TRANSPORT=tor` errors** → the daemon can't read Tor's
  control cookie. Confirm the control port is enabled (step 1) and your user is in the
  `debian-tor` group (`groups | grep debian-tor`); log out/in if you just added it.
- **`receivable: false` on phoenixd** → the channel cliff above; send the one-time inbound.
- **Host doesn't appear in a client's `--list`** → confirm the daemon logged `accepted by N
  relay(s)`; relay propagation can lag a re-announce (every ~300s). Make sure your
  `NOSTR_RELAYS` accept kind-38111 events.
- **Empty/error from `/api/status` right after a restart** → you queried before the daemon
  finished starting; wait for `systemctl is-active sail-host` and retry.
- **Model fails to load / OOM** → the model is too big for your VRAM; pick a smaller one
  (`nvidia-smi` to see usage).
