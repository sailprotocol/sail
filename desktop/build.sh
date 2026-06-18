#!/usr/bin/env bash
# Build the inference-net desktop app (Linux: .deb + AppImage).
# Prereqs (see desktop/README.md): .venv with pyinstaller; rustup + tauri-cli on PATH;
# apt: libwebkit2gtk-4.1-dev build-essential curl wget file libssl-dev
#      libayatana-appindicator3-dev librsvg2-dev
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
. "$HOME/.cargo/env" 2>/dev/null || true

echo "[1/4] PyInstaller sidecar (onedir)…"
.venv/bin/pyinstaller desktop/sidecar/server.spec --noconfirm \
  --distpath desktop/sidecar/dist --workpath desktop/sidecar/build

echo "[2/5] stage sidecar into Tauri resources…"
rm -rf desktop/src-tauri/resources/server
mkdir -p desktop/src-tauri/resources
cp -r desktop/sidecar/dist/server desktop/src-tauri/resources/server

echo "[3/5] fetch + stage Tor (tor-expert-bundle)…"
TOR_VERSION="${TOR_VERSION:-15.0.16}"   # bump as Tor releases; see archive.torproject.org
if [ ! -x desktop/src-tauri/resources/tor/tor ]; then
  tmp="$(mktemp -d)"
  url="https://archive.torproject.org/tor-package-archive/torbrowser/${TOR_VERSION}/tor-expert-bundle-linux-x86_64-${TOR_VERSION}.tar.gz"
  echo "  $url"
  curl -fsSL "$url" -o "$tmp/teb.tgz"
  tar -xzf "$tmp/teb.tgz" -C "$tmp"
  rm -rf desktop/src-tauri/resources/tor
  cp -r "$tmp/tor" desktop/src-tauri/resources/tor   # tor binary + bundled libs + pluggable_transports
  chmod +x desktop/src-tauri/resources/tor/tor
  rm -rf "$tmp"
fi

echo "[4/5] icons…"
if [ ! -f desktop/src-tauri/icons/icon.png ]; then
  .venv/bin/python desktop/make_icon.py desktop/icon-src.png
  ( cd desktop/src-tauri && cargo tauri icon ../icon-src.png )
fi

echo "[5/5] tauri build…"
( cd desktop/src-tauri && cargo tauri build )
echo "Done -> desktop/src-tauri/target/release/bundle/"
