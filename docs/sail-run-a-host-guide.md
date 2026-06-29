# Run a SAIL host

Serve an open model behind a Lightning paywall and earn sats per token. A host runs the SAIL
inference daemon behind your own Lightning backend and a Tor onion service, discovered over
Nostr. This guide takes you from a fresh Ubuntu box to a live, earning host.

> **Set expectations first.** Inference is served over Tor, so responses are **slow by
> design** (a short answer can take ~25–125s depending on model size). That latency is the
> privacy/sovereignty trade — your IP stays hidden and there's no central endpoint. It is not
> a bug. If you want speed over privacy, this isn't the right tool.

---

## Two ways to set up

**Fastest — the install script.** It automates all the system prep below (GPU check, Ollama,
Tor control port, repo/venv/deps, model selection) and hands you off to the setup wizard. It's
meant to be **downloaded and read before you run it** — not piped blindly into a shell.

```bash
# download it:
curl -fsSL https://raw.githubusercontent.com/sailprotocol/sail/master/scripts/install-host.sh -o install-host.sh
# READ it (recommended — it runs sudo for driver/packages/Tor):
less install-host.sh
# run it:
chmod +x install-host.sh
./install-host.sh
```

It prints a plan and asks you to confirm before doing anything, uses sudo only where needed,
never reboots for you, and is safe to re-run. When it finishes it prints the command to start
your host and the wizard URL. **One thing it can't do for you:** after it adds you to the
`debian-tor` group, you must **log out and back in** before starting the host (the guide
explains why under the Tor section).

**By hand — the manual steps below.** Prefer to understand/audit each step, or not run a
script? Follow the rest of this guide; it's the exact sequence the script automates.

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

First check whether you already have a working GPU driver:

```bash
nvidia-smi          # if this lists your GPU, skip the driver install below
```

If `nvidia-smi` works, skip ahead to Ollama. If it doesn't, install the driver — **a fresh
driver install needs a reboot** before `nvidia-smi` works (the kernel module loads on boot):

```bash
sudo ubuntu-drivers autoinstall
sudo reboot                         # reboot, then come back and re-run nvidia-smi to confirm
```

> ⚠️ That `reboot` will restart your machine. Only run it if you just installed the driver.
> After rebooting, continue here.

Then install Ollama, Tor, and Python:

```bash
# Ollama (serves the model locally):
curl -fsSL https://ollama.com/install.sh | sh
ollama --version

# Tor + Python tooling:
sudo apt update && sudo apt install -y tor python3-venv python3-pip git
```

### Enable Tor's control port (required)

The host creates its `.onion` service through Tor's control port. You need three directives in
`/etc/tor/torrc` — but they must appear **exactly once**.

> ⚠️ **Do not blindly append this block.** If `ControlPort 9051` is already in `torrc` and you add
> it a second time, Tor gets **duplicate directives and fails to start entirely** (`Could not bind
> ... Address already in use` → `Failed to bind one of the listener ports`). The failure is
> deceptive: `systemctl is-active tor` still reports `active` (that's the multi-instance *master*,
> not the real instance), nothing listens on 9051, and the daemon later dies with an opaque
> `ConnectionRefusedError`. This was the single biggest time-sink in testing — follow the steps in
> order and **verify** before moving on.

**1. Check whether the control port is already configured:**

```bash
grep -nE '^\s*ControlPort\s+9051' /etc/tor/torrc
```

- **Prints a line** → it's already set. **Skip step 2.** Open `/etc/tor/torrc` and make sure each
  of the three directives below appears only once (remove any duplicates), then go to step 3.
- **Prints nothing** → add the block once, in step 2.

**2. Add the directives (only if step 1 printed nothing):**

```bash
sudo tee -a /etc/tor/torrc >/dev/null <<'EOF'
ControlPort 9051
CookieAuthentication 1
CookieAuthFileGroupReadable 1
EOF
```

If you'd rather edit by hand (`sudo nano /etc/tor/torrc`), add those three lines once at the end —
and confirm they aren't already present higher up the file.

**3. Restart Tor and VERIFY the control port is actually listening:**

On Ubuntu, `tor.service` is a multi-instance *master*; it being "active" tells you **nothing** about
whether the control port came up. The real instance is `tor@default`.

```bash
sudo systemctl restart tor
sudo ss -ltnp | grep 9051        # MUST print a LISTEN line (127.0.0.1:9051). Empty = not up.
```

If `ss` prints nothing, the control port did not start — almost always duplicate directives. Find
out why, fix `torrc`, restart, and re-check:

```bash
sudo journalctl -u tor@default -n 30 --no-pager   # look for "Address already in use" / "Failed to bind"
```

**Do not continue until `ss ... grep 9051` shows a LISTENer.**

**4. Let the daemon read Tor's auth cookie — then RE-LOGIN (mandatory):**

```bash
sudo usermod -aG debian-tor "$USER"
```

> ⚠️ This does **not** apply to your current shell. You **must log out and back in** (or reboot) for
> the group to take effect — otherwise the daemon can't read Tor's auth cookie and onion creation
> fails. After re-login, verify:

```bash
groups | grep debian-tor         # must list "debian-tor"; if not, you haven't re-logged in yet
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

Copy the template. The defaults are already set for a real host — Tor transport, Nostr
discovery on public relays, and `PAYMENTS=mock` (which boots a fresh host straight into the
setup wizard, where you pick your real payout backend). You only need to set your model.

```bash
cp .env.example .env.host
```

Open `.env.host` and set the one thing that's specific to you:

- `OLLAMA_MODEL=<a model that fits your VRAM>` (e.g. `llama3.2:3b` for ~4 GB, `qwen3:14b` for ~14 GB)

That's it — `TRANSPORT=tor`, `REGISTRY=nostr`, and the public `NOSTR_RELAYS` are already the
defaults. Identity, onion key, and payout are created/collected for you on first run and in
the wizard.

> **Local dev?** If you're just testing without Tor or relays, the template has commented
> opt-outs: set `TRANSPORT=clearnet` and `REGISTRY=local`. Most operators leave the defaults.
>
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

> **Restarting the wizard.** The wizard *is* the daemon — to restart it, just re-run the `uvicorn`
> command above (Ctrl-C to stop it first if it's still running), then reopen
> `http://localhost:8001/setup`. Your progress is written to `.env.host` as you go, so a restart
> picks up where you left off. **Note:** on startup the daemon creates its onion, so it won't come
> up until Tor's control port is ready — if it exits immediately, re-check the
> [Tor control-port verify step](#enable-tors-control-port-required) (`ss ... grep 9051`).

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

- **Daemon/wizard won't start, or `ConnectionRefusedError` / `TRANSPORT=tor` errors** → Tor's
  control port isn't actually up. First verify: `sudo ss -ltnp | grep 9051` must show a LISTENer.
  If it's empty, Tor didn't bind the control port — the usual cause is **duplicate directives** in
  `/etc/tor/torrc` (the block was added more than once), which makes Tor fail to start *even though*
  `systemctl is-active tor` says "active". Check `sudo journalctl -u tor@default -n 30 --no-pager`
  for `Address already in use` / `Failed to bind`, remove the duplicate lines, `sudo systemctl
  restart tor`, and re-run the `ss` check. See [Enable Tor's control port](#enable-tors-control-port-required).
- **Wizard creates the onion but `groups` lacks `debian-tor`** → you added the group but haven't
  re-logged in. `sudo usermod -aG debian-tor "$USER"` does not affect the current session — log out
  and back in (or reboot), then confirm with `groups | grep debian-tor`.
- **Wizard says "no NVIDIA GPU detected" but `nvidia-smi` works** → you likely installed the driver
  without rebooting/relogging. The wizard reads the GPU at startup; reboot (or log out/in) after a
  driver install, then re-run the daemon so it re-detects.
- **`receivable: false` on phoenixd** → the channel cliff above; send the one-time inbound.
- **Host doesn't appear in a client's `--list`** → confirm the daemon logged `accepted by N
  relay(s)`; relay propagation can lag a re-announce (every ~300s). Make sure your
  `NOSTR_RELAYS` accept kind-38111 events.
- **Empty/error from `/api/status` right after a restart** → you queried before the daemon
  finished starting; wait for `systemctl is-active sail-host` and retry.
- **Model fails to load / OOM** → the model is too big for your VRAM; pick a smaller one
  (`nvidia-smi` to see usage).
