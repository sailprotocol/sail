# SAIL — dry-run pass 1 findings

## STATUS (updated)

Pass 1's punch-list is largely cleared. The original findings are preserved below for the record;
this section is the current state.

**Milestones:** **pass 2** (manual path) and **pass 3** (install script) each got a doc-only
fresh user all the way to a **live host**. The recruit-ready bar is met.

**Resolved & verified on hardware:**
- **F1** — Tor control-port guide rewrite (dedup-safe + verify). Merged `9cf9161`; verified pass 2 & 3.
- **F2** — go-live rendered the wrong user/workdir. Merged; verified (fresh identities published).
- **F3** — phoenixd silent provisioning failure + seed-ceremony gating. Merged; verified.
- **F3b** — scrubbed personal/regtest paths from the public `.env.example`. Merged `0ae7feb`.
- **F6, F7** — GPU "no device detected" note + restart-the-wizard note. Merged in the F1 commit.
- **F9** — `.env.example` real-operator defaults (TRANSPORT=tor, REGISTRY=nostr). Merged `d844bc0`.
- **F10** — reboot footgun in the guide. Merged `6a620a9`.
- **F12** — VRAM-aware model suggestion. Done via the install script.
- **F11** — don't publish a discoverable listing until go-live succeeds. Merged `174cebb`. Gates the public-relay announce (startup + heartbeat) while the host is still in setup (`PAYMENTS=mock`); the local registry is never withheld, so dev/test keeps working.
- **F14** — partial-relay-publish messaging. Merged `174cebb`. Reads as "published to N of M relay(s)" with stragglers a soft retry note; the scary warning fires only when ZERO relays accept.
- **Install script** (`scripts/install-host.sh`) — automated host setup. Merged `91505b5`; verified end-to-end pass 3.
- **Dual-method guide section** ("Two ways to set up"). Merged `19bcb8c`.

**Remaining (polish, non-blocking — pass 2 & 3 did not hit these):**
- **F4** — name the real failure cause in the wizard (port-in-use / Tor-not-ready / phoenixd-not-provisioned), not a bare "failed".
- **F5** — daemon hard-crashes when Tor isn't ready; let it start and serve the wizard so the user can fix + retry.
- **F8** — support links (Telegram + email) in the guide. Blocked on the Telegram handle.

---

Standing up a host as a **fresh, doc-only `sailtest` user** on a clean profile (NucBox),
following only the README + `docs/sail-run-a-host-guide.md`. The goal was a snag-free
start-to-finish host setup; we did not reach it, and every snag below is a gap a real
operator #1 would hit. This is the punch-list to fix before pass 2.

Priority: **P0 blockers** (a recruit cannot complete setup) → **P1** (dangerous/opaque) →
**P2** (polish). Fix P0s first; pass 2 should then get much further.

---

## P0 — Blockers (stop a recruit cold)

### F2. Wizard go-live renders the WRONG user/workdir, and the unit file isn't written
Running as `sailtest` from `/home/sailtest/sail`, the go-live screen displayed:
`sudo cp /home/rob/dev/sail/deploy/sail-host.service /etc/systemd/system/...`
— i.e. **`rob`'s** path, not the running user's. And `cat ~/sail/deploy/sail-host.service`
returned nothing — **the unit was never actually rendered** for this user.
- Fix: `service_setup` / go-live must derive `{{USER}}` and `{{WORKDIR}}` (and `{{ENV_FILE}}`,
  `{{UVICORN}}`) from the **running process's** environment, not a hardcoded/rob-specific
  value. Actually write `deploy/sail-host.service` before telling the user to `cp` it.
- A recruit copying the shown command would install rob's unit or fail. Blocker.

### F3. phoenixd provisioning fails silently, but the wizard advances anyway
Picked phoenixd in the payout step; go-live then showed
`phoenixd unreachable: [Errno 111] Connection refused`. On disk: **no `~/.phoenix`, no
phoenixd service, no data** — provisioning never completed. Yet the wizard still advanced to
go-live.
- Fix: the payout step must actually provision phoenixd for a fresh user (download/first-run/
  write config+password), and if it FAILS, surface the real error and **do not advance** to a
  go-live that can't possibly receive.
- **Linked seed-phrase issue:** the wizard showed a 12-word seed + "verify two words" ceremony,
  but no phoenixd wallet persisted on disk afterward. So a user may carefully record a seed for
  a wallet **that was never created**. The seed must only be shown for a wallet that is actually
  generated and persisted; verify provisioning succeeded before/around the seed ceremony.
- (Earlier this session phoenixd provisioned fine on host #2 as `rob` — so this is likely a
  fresh-user/permissions/privilege gap in the provision path. Investigate why it works for rob
  but not a clean user.)

---

## P1 — Dangerous or opaque

### F1. Tor control-port setup is dangerous as written (the big one)
The guide's 3-line torrc block, if appended more than once (re-running the guide, or unclear
it's already done), **duplicates the directives → Tor fails to start entirely** (`Could not
bind ... Address already in use`, `Failed to bind one of the listener ports`). Result: an
opaque downstream failure (`ConnectionRefusedError` in the daemon) with a "running" service
and a reachable-looking URL masking a dead Tor. This cost the most time in the dry run.
- Fix the guide to:
  - Use a **dedup-safe** approach (check-before-append, or instruct editing not appending),
    and warn that duplicate directives make Tor fail to start.
  - Add a mandatory **verify** step: `sudo ss -ltnp | grep 9051` must show a LISTENer before
    proceeding.
  - State the correct service model (Ubuntu uses `tor.service` multi-instance master; the real
    instance is `tor@default` — and "active" on the master does NOT mean the control port is up).
  - Make the **`debian-tor` group re-login mandatory and loud** — `usermod -aG debian-tor`
    does NOT apply to the current session; you must log out/in or the daemon can't read the
    auth cookie.

### F4. Failures don't surface their real cause
Multiple distinct failures all presented as opaque "self-test failed" / "unreachable":
port-in-use (`[Errno 98]`), Tor-not-ready, phoenixd-not-provisioned. The actionable reason was
only in the daemon log, not the wizard UI.
- Fix: the wizard self-test / go-live should detect and name the cause — "port 8001 in use,"
  "Tor control port not reachable," "phoenixd not provisioned" — with the fix, instead of a
  bare "failed."

### F5. Daemon hard-crashes when Tor isn't ready
`publish_listing` calls `setup_onion` at startup; if Tor's control port isn't up, the daemon
throws and **exits** (`Application startup failed`). So a fresh operator can't even reach the
wizard's Detect screen (which has a Tor check) to learn what's wrong.
- Fix: let the daemon start and serve the wizard even if Tor isn't ready; surface the Tor
  state in the wizard and let the user fix + retry, rather than crashing on boot.

### F11. Half-finished wizard runs publish ghost listings to public relays
The abandoned `sailtest` wizard run published `sneaking-cargo · fcf7` (qwen3:14b) to the public
relays before go-live succeeded — it now shows in clients' `--list` as a host that can't serve.
(Same class as the lingering `hooded-guilt · 51ab` test ghost.)
- Fix: don't publish a discoverable 38111 listing until the host is actually live-to-serve
  (post go-live), so an incomplete/abandoned setup doesn't litter the relays.

---

## P2 — Polish / docs

### F6. GPU detection after a driver install
The wizard reported "no NVIDIA GPU detected" until a driver install + **reboot/relogin**, even
though `nvidia-smi` later worked. Document: if the wizard says no GPU but `nvidia-smi` works,
reboot/relogin after the driver install.

### F7. "How to restart the wizard" isn't documented
The wizard *is* the daemon; it's restarted by re-running the uvicorn command (and it crashes if
Tor isn't up — see F5). Add this to the guide.

### F8. Support channels
Add a "Stuck? Ask in [Telegram] or email sailprotocol@protonmail.com" footer to the guide's
troubleshooting section + a "stuck" link where relevant. *(Telegram handle pending — Rob is
creating it.)*

### F9. `.env.example` defaults — DONE ✅
Flipped to the real operator path (TRANSPORT=tor, REGISTRY=nostr, public relays) with dev
opt-out comments. Merged.

### F10. Reboot footgun in the guide — DONE ✅
Step 1's `&& sudo reboot` removed; reboot is now conditional + clearly flagged. Merged.

---

## Suggested fix order
1. **F2 + F3** (P0 go-live blockers) — without these no one completes setup.
2. **F1** (Tor guide rewrite) — biggest time-sink, doc-side.
3. **F4 + F5** (surface real failures; don't crash on Tor-not-ready).
4. **F11** (don't publish ghosts pre-go-live).
5. **F6 + F7 + F8** (doc polish + support links).

Then **dry-run pass 2** — fresh user, doc-only — aiming for snag-free start-to-finish. That's
the run to record and recruit against.
