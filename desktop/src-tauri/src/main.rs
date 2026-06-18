// inference-net desktop shell.
//
// Launches the bundled Python client (client.webapp via the PyInstaller --onedir "server"
// binary) as a child process, waits for it to come up on a free localhost port, then opens a
// webview pointed at it. All client state lives in the OS app-data dir (not the repo). The
// child is killed when the app exits.
//
// The sidecar onedir ships via bundle.resources (a binary + _internal/ dir), so we spawn it
// from the resolved resource path rather than Tauri's single-file `externalBin`/sidecar API.
// Set SIDECAR_BIN to override the path for `tauri dev`.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct Sidecar(Mutex<Option<CommandChild>>);

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8765)
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();

            // App-data dir holds all client state (NWC string, history, reputation, registry).
            let data = app.path().app_data_dir()?;
            std::fs::create_dir_all(data.join("registry")).ok();

            // Bundled Python sidecar (onedir). SIDECAR_BIN overrides for `tauri dev`.
            let sidecar_bin = std::env::var("SIDECAR_BIN")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| {
                    app.path()
                        .resource_dir()
                        .expect("resource_dir")
                        .join("server")
                        .join("server")
                });

            let port = free_port();
            let s = |p: std::path::PathBuf| p.to_string_lossy().to_string();
            let mut env: HashMap<String, String> = HashMap::new();
            env.insert("REGISTRY".into(), "nostr".into());
            env.insert("NOSTR_RELAYS".into(), "wss://relay.damus.io".into());
            env.insert("PAYMENTS".into(), "nwc".into());
            env.insert("REGISTRY_DIR".into(), s(data.join("registry")));
            env.insert("NWC_PATH".into(), s(data.join("client_nwc.json")));
            env.insert("HISTORY_PATH".into(), s(data.join("client_history.json")));
            env.insert("REPUTATION_PATH".into(), s(data.join("client_reputation.json")));

            let (mut rx, child) = app
                .shell()
                .command(sidecar_bin)
                .args([port.to_string()])
                .envs(env)
                .spawn()?;
            *app.state::<Sidecar>().0.lock().unwrap() = Some(child);

            // Surface sidecar logs to the shell's stderr.
            tauri::async_runtime::spawn(async move {
                while let Some(ev) = rx.recv().await {
                    if let CommandEvent::Stdout(b) | CommandEvent::Stderr(b) = ev {
                        eprintln!("[sidecar] {}", String::from_utf8_lossy(&b).trim_end());
                    }
                }
            });

            // Wait for the server to listen, then open the window pointed at it.
            std::thread::spawn(move || {
                for _ in 0..150 {
                    if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                        std::thread::sleep(Duration::from_millis(400)); // let uvicorn finish startup
                        let url = format!("http://127.0.0.1:{port}");
                        if let Err(e) = WebviewWindowBuilder::new(
                            &handle,
                            "main",
                            WebviewUrl::External(url.parse().unwrap()),
                        )
                        .title("inference-net")
                        .inner_size(1120.0, 780.0)
                        .build()
                        {
                            eprintln!("[shell] window build failed: {e}");
                        }
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                eprintln!("[shell] sidecar did not become ready in time");
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building tauri app")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(child) = app_handle.state::<Sidecar>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
