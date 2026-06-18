# PyInstaller spec for the desktop sidecar.
# Run from anywhere:  .venv/bin/pyinstaller desktop/sidecar/server.spec
# Produces dist/server/  (server binary + _internal/ with libs and client/static).
# --onedir (not onefile): reliable loading of nostr_sdk's native lib, and a single process
# so the Tauri shell's kill-on-exit doesn't orphan the server.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Paths in a spec resolve relative to the spec's dir (SPECPATH); anchor on the repo root.
REPO = os.path.abspath(os.path.join(SPECPATH, os.pardir, os.pardir))

datas = [(os.path.join(REPO, "client", "static"), "client/static")]  # FileResponse(_STATIC/"index.html")
binaries = []
hiddenimports = ["client.webapp"] + collect_submodules("uvicorn")

# nostr_sdk ships a compiled libnostr_sdk_ffi.so — collect_all grabs the binary + package data.
for pkg in ("nostr_sdk",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(REPO, "desktop", "sidecar", "server.py")],
    pathex=[REPO],           # so `client`, `shared`, `host` import
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="server",
    console=True,
)
coll = COLLECT(exe, a.binaries, a.datas, name="server")
