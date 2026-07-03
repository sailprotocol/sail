# SAIL operator cheatsheet

The two machines and the commands you run on each. **Always check the prompt** —
`rob@rob-NucBox-K11` vs `rob@rob-frontroom` — before running.

## The two boxes
| Box | Hostname | Role | Host identity | Model | Payments |
|-----|----------|------|---------------|-------|----------|
| **NucBox** | `rob-NucBox-K11` | **host #1** + **the client** | `winged-ministry · 2a2f` | qwen3:14b | LND |
| **1050 Ti** | `rob-frontroom` | **host #2** | `jealous-delta · 1175` | llama3.2:3b | phoenixd |

Both run the host as the systemd service `sail-host`. The **client lives on the NucBox**.

---

## Host service — run ON the box you're controlling
```bash
sudo systemctl restart sail-host         # restart the host (after a git pull, or to apply config)
systemctl is-active sail-host            # → active   (quick "is it up?")
sudo systemctl stop sail-host            # take the host offline
sudo systemctl start sail-host           # bring it back
journalctl -u sail-host -f               # WATCH the host log live (Ctrl-C to stop)
journalctl -u sail-host --since "5 min ago" --no-pager   # recent host log, no tail
```
> Use `-u sail-host` — plain `journalctl -f` is the whole-system firehose (cups/timekpr noise).

## Update a box to the latest code  (run ON that box)
```bash
cd ~/dev/sail && git pull                # pull merged master
sudo systemctl restart sail-host         # then restart so the service runs the new code
```

## phoenixd — ON the 1050 Ti (host #2) only
```bash
sudo systemctl restart phoenixd          # restart the Lightning node
systemctl is-active phoenixd             # → active
PW=$(grep '^PHOENIXD_API_PASSWORD=' ~/dev/sail/.env.host | cut -d= -f2)
curl -s -u ":$PW" http://127.0.0.1:9740/getbalance; echo      # balanceSat / feeCreditSat
curl -s -u ":$PW" http://127.0.0.1:9740/listchannels; echo    # [] = no channel
```

## Client — ON the NucBox only
```bash
# GUI (foreground — stderr step-logs print in THIS terminal):
ENV_FILE=.env.client PYTHONPATH=. .venv/bin/uvicorn client.webapp:app --port 8090
#   → open http://localhost:8090   (Ctrl-C to stop)

# list hosts from the CLI (ALWAYS pass ENV_FILE=.env.client — without it REGISTRY
# defaults to 'local' and you'll see stale ./registry test listings, not the relays):
ENV_FILE=.env.client PYTHONPATH=. .venv/bin/python -m client.cli --list

# local reputation (if a host gets hidden):
ENV_FILE=.env.client PYTHONPATH=. .venv/bin/python -m client.cli --reputation        # inspect
ENV_FILE=.env.client PYTHONPATH=. .venv/bin/python -m client.cli --reset-reputation  # clear

# clear stale local-dir test listings (mock/localhost) if you ever ran --list without ENV_FILE:
PYTHONPATH=. .venv/bin/python -m client.cli --purge-local-registry        # (--all = wipe the dir)
```

## When a port is "address already in use"  (run ON that box)
```bash
pkill -f 'uvicorn host.daemon'   ;  sleep 1     # kill a stray HOST process
pkill -f 'uvicorn client.webapp' ;  sleep 1     # kill a stray CLIENT process
ss -ltn 'sport = :8001'                          # should print no LISTEN line = port free
sudo ss -ltnp 'sport = :8001'                    # add sudo to see the PID holding it
```

## Sanity checks  (run ON the box)
```bash
pgrep -af 'uvicorn host.daemon'    # is a host daemon running? (one line expected)
pgrep -af phoenixd                 # is phoenixd running? (1050 Ti)
nvidia-smi                         # GPU + VRAM use
curl -s localhost:8092/api/status; echo   # this host's own status JSON (operator port, local only)
```

## Public relays both hosts use
`wss://relay.damus.io,wss://nos.lol`  — set in each box's `.env.host` (`NOSTR_RELAYS`)
and the client's `.env.client`.
