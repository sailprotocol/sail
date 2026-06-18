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

echo "[2/4] stage sidecar into Tauri resources…"
rm -rf desktop/src-tauri/resources/server
mkdir -p desktop/src-tauri/resources
cp -r desktop/sidecar/dist/server desktop/src-tauri/resources/server

echo "[3/4] icons…"
if [ ! -f desktop/src-tauri/icons/icon.png ]; then
  .venv/bin/python desktop/make_icon.py desktop/icon-src.png
  ( cd desktop/src-tauri && cargo tauri icon ../icon-src.png )
fi

echo "[4/4] tauri build…"
( cd desktop/src-tauri && cargo tauri build )
echo "Done -> desktop/src-tauri/target/release/bundle/"
