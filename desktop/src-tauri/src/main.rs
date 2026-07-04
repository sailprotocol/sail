// SAIL (Sovereign AI Inference Layer) desktop shell.
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

// All spawned children (Tor + Python sidecar); killed together on exit.
struct Children(Mutex<Vec<CommandChild>>);

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8765)
}

/// Bundled resources can lose their exec bit when copied into the package; restore it.
fn ensure_exec(path: &std::path::Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = std::fs::metadata(path) {
            let mut perm = meta.permissions();
            perm.set_mode(perm.mode() | 0o755);
            let _ = std::fs::set_permissions(path, perm);
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Children(Mutex::new(Vec::new())))
        .setup(|app| {
            let handle = app.handle().clone();
            let res_dir = app.path().resource_dir().ok();

            // App-data dir holds all client state (NWC string, history, reputation, registry, tor).
            let data = app.path().app_data_dir()?;
            std::fs::create_dir_all(data.join("registry")).ok();
            std::fs::create_dir_all(data.join("tor")).ok();

            let s = |p: std::path::PathBuf| p.to_string_lossy().to_string();
            let push_child = |c: CommandChild| app.state::<Children>().0.lock().unwrap().push(c);
            let drain = |mut rx: tauri::async_runtime::Receiver<CommandEvent>, tag: &'static str| {
                tauri::async_runtime::spawn(async move {
                    while let Some(ev) = rx.recv().await {
                        if let CommandEvent::Stdout(b) | CommandEvent::Stderr(b) = ev {
                            eprintln!("[{tag}] {}", String::from_utf8_lossy(&b).trim_end());
                        }
                    }
                });
            };

            // --- Tor: launch a SOCKS proxy so .onion hosts work in-app (Bisq-style). ---
            // The client routes .onion endpoints through TOR_SOCKS; clearnet/LAN works without it.
            // TOR_BIN overrides the bundled binary for `tauri dev` (e.g. the system tor).
            let tor_socks = free_port();
            let tor_bin = std::env::var("TOR_BIN").map(std::path::PathBuf::from).ok().or_else(|| {
                res_dir.as_ref().map(|r| r.join("tor").join("tor"))
            });
            if let Some(tor_bin) = tor_bin.filter(|p| p.exists()) {
                ensure_exec(&tor_bin);
                let torrc = data.join("tor").join("torrc");
                let _ = std::fs::write(&torrc, format!(
                    "SocksPort 127.0.0.1:{tor_socks}\nDataDirectory {}\nClientOnly 1\nAvoidDiskWrites 1\nLog notice stdout\n",
                    s(data.join("tor").join("data")),
                ));
                match app.shell().command(tor_bin).args(["-f", &s(torrc)]).spawn() {
                    Ok((rx, child)) => { drain(rx, "tor"); push_child(child); }
                    Err(e) => eprintln!("[shell] tor failed to start (clearnet still works): {e}"),
                }
            } else {
                eprintln!("[shell] no tor binary found; .onion hosts will be unreachable");
            }

            // --- Python sidecar (the existing client.webapp). ---
            let sidecar_bin = std::env::var("SIDECAR_BIN")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| {
                    res_dir.clone().expect("resource_dir").join("server").join("server")
                });

            let port = free_port();
            let mut env: HashMap<String, String> = HashMap::new();
            env.insert("REGISTRY".into(), "nostr".into());
            // Two relays, not one: a single relay is a discovery SPOF — if it's slow to connect on a
            // cold start the 5s fetch can return empty ("No hosts found") even when a host is live.
            env.insert("NOSTR_RELAYS".into(), "wss://relay.damus.io,wss://nos.lol".into());
            env.insert("PAYMENTS".into(), "nwc".into());
            env.insert("TOR_SOCKS".into(), format!("socks5h://127.0.0.1:{tor_socks}"));
            env.insert("REGISTRY_DIR".into(), s(data.join("registry")));
            env.insert("NWC_PATH".into(), s(data.join("client_nwc.json")));
            env.insert("HISTORY_PATH".into(), s(data.join("client_history.json")));
            env.insert("REPUTATION_PATH".into(), s(data.join("client_reputation.json")));

            ensure_exec(&sidecar_bin);
            let (rx, child) = app.shell().command(sidecar_bin).args([port.to_string()]).envs(env).spawn()?;
            push_child(child);
            drain(rx, "sidecar");

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
                        .title("SAIL")
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
                for child in app_handle.state::<Children>().0.lock().unwrap().drain(..) {
                    let _ = child.kill();
                }
            }
        });
}
