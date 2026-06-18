# inference-net desktop app (Tauri)

A standalone desktop client (Bisq-style): one app that runs the existing Python client
(`client.webapp`) as a bundled sidecar and loads its UI in a native webview. No Python, no
separate server, no browser. The local-web-app path (`uvicorn client.webapp:app`) still works
for dev — this just packages it.

## How it works
- **Sidecar** (`desktop/sidecar/`): PyInstaller `--onedir` bundles `client.webapp:app` (FastAPI +
  uvicorn + the native `nostr_sdk` lib) into a self-contained `server` binary.
- **Shell** (`desktop/src-tauri/`): the Rust app picks a free localhost port, spawns the sidecar
  with config env pointing at the OS **app-data dir** (`~/.local/share/net.inference.client/`),
  waits for it to listen, then opens a webview at `http://127.0.0.1:<port>`. The sidecar is killed
  on exit.
- **Tor** (phase C): a bundled `tor` is launched and `TOR_SOCKS` is set so `.onion` hosts work
  out of the box (the client already routes `.onion` via Tor's SOCKS proxy).

## Prerequisites (Linux / Ubuntu 24.04)
```bash
# Rust + Tauri CLI (user-level)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env" && cargo install tauri-cli --version '^2.0' --locked
# Python bundler
.venv/bin/pip install pyinstaller
# Tauri system libs (sudo)
sudo apt install -y libwebkit2gtk-4.1-dev build-essential curl wget file libssl-dev \
  libayatana-appindicator3-dev librsvg2-dev
```

## Dev (fast iteration, no full bundle)
```bash
# Build the sidecar once, then point the shell at it and run:
.venv/bin/pyinstaller desktop/sidecar/server.spec --noconfirm \
  --distpath desktop/sidecar/dist --workpath desktop/sidecar/build
SIDECAR_BIN="$PWD/desktop/sidecar/dist/server/server" \
  cargo tauri dev --config desktop/src-tauri/tauri.conf.json   # (run from desktop/src-tauri)
```

## Build installers (.deb + AppImage)
```bash
./desktop/build.sh        # sidecar -> stage resources -> icons -> tauri build
# -> desktop/src-tauri/target/release/bundle/{deb,appimage}/
```

## Cross-platform
`externalBin`/resources and the Tor binary are per-OS; macOS/Windows = add their sidecar + Tor
binaries and run `tauri build` on that OS. No architecture change.
