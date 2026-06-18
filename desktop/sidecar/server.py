"""
Desktop sidecar entrypoint.

Runs the existing FastAPI web client (client.webapp:app) as a self-contained localhost server
for the Tauri desktop shell to load in its webview. Bundled with PyInstaller so the end user
needs no Python installed. All config (NWC store, history, reputation, registry dir, relays,
payment mode) comes from env vars the shell injects, pointing at the app-data dir.

    server [port]   # default 8765

Importing `app` directly (not the "client.webapp:app" import string) lets PyInstaller's static
analysis follow the dependency graph into client/* and nostr_sdk.
"""
import sys

import uvicorn

from client.webapp import app


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
