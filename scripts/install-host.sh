#!/usr/bin/env bash
#
# install-host.sh — automated SAIL host setup (the fast path).
#
# This encodes the exact, verified sequence from docs/sail-run-a-host-guide.md so you don't have
# to run the manual steps by hand. It is meant to be DOWNLOADED AND READ before you run it — it is
# not a curl|bash one-liner. It does only system prep + repo/venv/env setup, then hands off to the
# in-browser setup wizard for the SAIL-specific choices (model pull, pricing, payout, go-live).
#
# It is safe to re-run: every step checks whether it's already done.
#
#   Usage:  bash scripts/install-host.sh
#
# Requires: Ubuntu (apt). Uses sudo ONLY for the specific commands that need root (driver,
# packages, torrc, group) — the script itself does not run as root.

set -euo pipefail

# ── pretty output ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  B=$'\e[1m'; DIM=$'\e[2m'; GRN=$'\e[32m'; YLW=$'\e[33m'; RED=$'\e[31m'; RST=$'\e[0m'
else
  B=""; DIM=""; GRN=""; YLW=""; RED=""; RST=""
fi
say()   { printf '%s\n' "$*"; }
step()  { printf '\n%s== %s ==%s\n' "$B" "$*" "$RST"; }
ok()    { printf '%s  ✓ %s%s\n' "$GRN" "$*" "$RST"; }
warn()  { printf '%s  ! %s%s\n' "$YLW" "$*" "$RST"; }
die()   { printf '%s  ✗ %s%s\n' "$RED" "$*" "$RST" >&2; exit 1; }

# ask "Prompt" "default" -> echoes the chosen value (enter accepts default; non-tty uses default)
ask() {
  local prompt="$1" default="${2:-}" reply
  if [[ ! -t 0 ]]; then printf '%s\n' "$default"; return; fi
  read -r -p "$prompt [$default]: " reply || true
  printf '%s\n' "${reply:-$default}"
}
confirm() {  # confirm "Question" -> 0 if yes (default yes)
  local reply; [[ -t 0 ]] || return 0
  read -r -p "$1 [Y/n]: " reply || true
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# set KEY=value in .env.host (replace in place or append) — values here are simple tokens/paths.
set_env_var() {
  local key="$1" val="$2" file="$3"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}

# ── header: explain everything BEFORE doing anything ─────────────────────────
cat <<EOF
${B}SAIL host — automated setup${RST}

This script will, in order, on this machine:
  1. Check the NVIDIA GPU driver (and install it if missing — then ask you to reboot).
  2. Install Ollama, Tor, and Python tooling (apt).
  3. Enable Tor's control port in /etc/tor/torrc (dedup-safe) and verify it's listening.
  4. Add your user to the 'debian-tor' group (you'll need to log out/in afterwards).
  5. Get the SAIL repo, create a Python venv, and install dependencies.
  6. Create .env.host from the template and pick a model that fits your VRAM.
  7. Print the command to start the daemon + open the setup wizard.

It uses ${B}sudo${RST} only for the specific steps that need root. It does NOT make any SAIL
payout/identity choices and does NOT reboot for you. It is safe to re-run.

The equivalent manual steps live in docs/sail-run-a-host-guide.md if you'd rather do them by hand.
EOF
confirm "Continue?" || die "Aborted."

command -v apt-get >/dev/null 2>&1 || die "This installer targets Ubuntu/Debian (apt not found). Use the manual guide."
# Cache sudo credentials once up front so later steps don't each prompt (does NOT run the script as root).
say ""; say "${DIM}sudo is needed for driver/packages/torrc/group; you may be prompted for your password now.${RST}"
sudo -v || die "sudo is required for system setup."

# ── 1. GPU driver ────────────────────────────────────────────────────────────
step "1/7  GPU driver"
if nvidia-smi >/dev/null 2>&1; then
  ok "nvidia-smi works — $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)"
else
  warn "No working NVIDIA driver detected."
  if confirm "Install it now with 'sudo ubuntu-drivers autoinstall'?"; then
    sudo ubuntu-drivers autoinstall
    say ""
    warn "Driver installed. You must ${B}REBOOT${RST}${YLW} now, then re-run this script.${RST}"
    say "  sudo reboot"
    exit 0
  else
    die "A working GPU driver is required. Install one and re-run."
  fi
fi

# ── 2. Ollama + Tor + Python ─────────────────────────────────────────────────
step "2/7  Ollama, Tor, Python tooling"
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama already installed ($(ollama --version 2>/dev/null | head -n1))"
else
  say "Installing Ollama (official installer)…"
  curl -fsSL https://ollama.com/install.sh | sh
  ok "Ollama installed."
fi
say "Installing Tor + Python tooling (apt)…"
sudo apt-get update -qq
sudo apt-get install -y tor python3-venv python3-pip git
ok "tor, python3-venv, python3-pip, git installed."

# ── 3. Tor control port (dedup-safe) + verify ────────────────────────────────
step "3/7  Tor control port"
TORRC=/etc/tor/torrc
# Add each directive only if its key is absent — never duplicate (duplicate directives make Tor
# fail to start entirely). This mirrors the dedup-safe logic in the run-a-host guide.
ensure_torrc() {
  local pattern="$1" line="$2"
  if sudo grep -qE "$pattern" "$TORRC" 2>/dev/null; then
    ok "already present: $line"
  else
    printf '%s\n' "$line" | sudo tee -a "$TORRC" >/dev/null
    ok "added: $line"
  fi
}
ensure_torrc '^[[:space:]]*ControlPort[[:space:]]+9051'             'ControlPort 9051'
ensure_torrc '^[[:space:]]*CookieAuthentication[[:space:]]+1'       'CookieAuthentication 1'
ensure_torrc '^[[:space:]]*CookieAuthFileGroupReadable[[:space:]]+1' 'CookieAuthFileGroupReadable 1'

say "Restarting Tor…"
sudo systemctl restart tor
# Verify the control port is ACTUALLY listening. On Ubuntu 'tor.service' is a multi-instance
# master — being 'active' says nothing about the control port. The real instance is tor@default.
sleep 1
if ss -ltn 2>/dev/null | grep -q ':9051[[:space:]]'; then
  ok "Tor control port is listening on 9051."
else
  warn "Tor control port is NOT listening on 9051."
  say  "  Tor likely failed to start — most often duplicate directives in $TORRC."
  say  "  Check the reason:"
  say  "    sudo journalctl -u tor@default -n 30 --no-pager   ${DIM}# look for 'Address already in use' / 'Failed to bind'${RST}"
  die  "Fix $TORRC (remove duplicate directives), then re-run this script."
fi

# ── 4. debian-tor group (re-login required) ──────────────────────────────────
step "4/7  debian-tor group"
GROUP_ACTIVE=0
if id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx debian-tor; then
  ok "'$USER' is in 'debian-tor' and it's active in this session."
  GROUP_ACTIVE=1
else
  if getent group debian-tor 2>/dev/null | grep -qE "[:,]$USER(,|$)"; then
    ok "'$USER' is already a member of 'debian-tor' (not yet active in this session)."
  else
    sudo usermod -aG debian-tor "$USER"
    ok "Added '$USER' to 'debian-tor'."
  fi
  say ""
  warn "${B}You must LOG OUT and back in (or reboot) before starting the host.${RST}"
  say  "  ${YLW}usermod does not affect this shell session — until you re-login, the daemon"
  say  "  cannot read Tor's auth cookie and onion creation will fail.${RST}"
  say  "  After re-login, verify with:  groups | grep debian-tor"
fi

# ── 5. Repo + venv + deps ────────────────────────────────────────────────────
step "5/7  Repo, venv, dependencies"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAND="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || true)"
REPO_URL="https://github.com/sailprotocol/sail.git"
if [[ -n "$CAND" && -f "$CAND/host/daemon.py" && -f "$CAND/.env.example" ]]; then
  REPO="$CAND"
  ok "Using the SAIL repo this script lives in: $REPO"
else
  DEST="$(ask "Clone SAIL to which directory?" "$HOME/sail")"
  DEST="${DEST/#\~/$HOME}"
  if [[ -d "$DEST/.git" && -f "$DEST/host/daemon.py" ]]; then
    ok "Repo already present at $DEST — using it."
  elif [[ -e "$DEST" && -n "$(ls -A "$DEST" 2>/dev/null)" ]]; then
    die "$DEST exists and is not a SAIL checkout. Pick another path or remove it."
  else
    say "Cloning $REPO_URL → $DEST …"
    git clone "$REPO_URL" "$DEST"
  fi
  REPO="$DEST"
fi
cd "$REPO"

if [[ -d .venv ]]; then
  ok ".venv already exists."
else
  say "Creating .venv …"
  python3 -m venv .venv
fi
say "Installing Python dependencies (this can take a minute)…"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
ok "Dependencies installed."

# ── 6. .env.host + model selection ───────────────────────────────────────────
step "6/7  Host config (.env.host) + model"
if [[ -f .env.host ]]; then
  ok ".env.host already exists — keeping it (and your current settings)."
else
  cp .env.example .env.host
  ok "Created .env.host from .env.example (defaults: TRANSPORT=tor, REGISTRY=nostr, PAYMENTS=mock)."
fi

# Suggest a model that fits this GPU's VRAM; let the user accept or override.
suggest_model() {
  local mib gb
  mib="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -dc '0-9')"
  if [[ -z "$mib" ]]; then printf 'qwen3:14b\n'; return; fi
  gb=$(( mib / 1024 ))
  if   (( gb < 6  )); then printf 'llama3.2:3b\n'
  else                     printf 'qwen3:14b\n'
  fi
}
# Default = current value in .env.host if set, else the VRAM-based suggestion.
CUR_MODEL="$(grep -E '^OLLAMA_MODEL=' .env.host | head -n1 | cut -d= -f2- || true)"
VRAM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -dc '0-9' || true)"
SUGGEST="${CUR_MODEL:-$(suggest_model)}"
if [[ -n "$VRAM_MIB" ]]; then
  say "Detected ~$(( VRAM_MIB / 1024 )) GB VRAM. Suggested model: ${B}$(suggest_model)${RST}"
  say "  ${DIM}(<6 GB → llama3.2:3b · otherwise → qwen3:14b · enter any Ollama model name to override)${RST}"
fi
MODEL="$(ask "Model to serve" "$SUGGEST")"
set_env_var "MODEL" "ollama" .env.host
set_env_var "OLLAMA_MODEL" "$MODEL" .env.host
ok "Set OLLAMA_MODEL=$MODEL in .env.host (the wizard will pull it if it isn't downloaded yet)."

# ── 7. Hand off to the wizard ────────────────────────────────────────────────
step "7/7  Next step — start the daemon and finish in the wizard"
cat <<EOF

${GRN}${B}System setup is complete.${RST}

Start the host daemon:

  ${B}cd $REPO${RST}
  ${B}ENV_FILE=.env.host PYTHONPATH=. .venv/bin/uvicorn host.daemon:app --port 8001${RST}

Then open the setup wizard in your browser (local only):

  ${B}http://localhost:8001/setup${RST}

The wizard handles the rest: pull the model, set pricing, pick your payout backend
(phoenixd / LND / NWC), back up your seed, and go live.
EOF

if [[ "$GROUP_ACTIVE" -eq 0 ]]; then
  say ""
  warn "Reminder: LOG OUT and back in (or reboot) FIRST — otherwise the daemon can't reach Tor"
  say  "          and onion creation will fail. After re-login: groups | grep debian-tor"
fi
