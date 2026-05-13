# Rustuya Manager

A management tool for [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) that synchronizes Tuya devices between the Tuya Cloud (via `tuyadevices.json`) and the running bridge. Ships with both a CLI mode and a web UI.

> [!TIP]
> Use **[tuyawizard](https://github.com/3735943886/tuyawizard)** to generate `tuyadevices.json` from your Tuya Cloud account.

## Key Features

- **Status dashboard** — Synced / Mismatched / Missing / Orphaned categories by diffing your cloud JSON against the bridge's live state.
- **No separate config** — picks up the bridge's topic and payload templates from its retained `bridge/config`.
- **Live updates over MQTT** — DPS values stream into the UI in real time.
- **Web UI + CLI** — single-page UI with drag-and-drop cloud-JSON upload, search, sort, sub-device tree, per-device add/edit/remove. CLI prints diff + event stream for SSH-style workflows.

## Quick Start

Requires Python 3.10+ and a running [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) reachable via MQTT.

### Install (pipx)

```bash
sudo apt install -y pipx                          # if not already
pipx ensurepath
pipx install rustuya-manager
```

This installs into an isolated venv at `~/.local/pipx/venvs/rustuya-manager/` and drops a `rustuya-manager` shim into `~/.local/bin/`.

### Run

CLI mode (diff + event stream to stdout):
```bash
rustuya-manager --broker mqtt://localhost:1883 --root rustuya
```

Web UI mode:
```bash
rustuya-manager --broker mqtt://localhost:1883 --root rustuya \
                --web --port 8080 --auth admin:CHANGE_ME
```
Then open the URL printed at startup. The default bind is `127.0.0.1` so the UI is reachable only from the same machine. To open it to the LAN add `--host 0.0.0.0` — pair with a real `--auth user:pass`.

Common flags:
- `--cloud PATH` (default `tuyadevices.json`) — Tuya devices JSON. If
  missing, the UI shows a drop-zone for upload.
- `--broker URL` (default `mqtt://localhost:1883`) — accepts
  `mqtt://[user:pass@]host:port`.
- `--root TOPIC` (default `rustuya`) — must match the bridge's
  `--mqtt-root-topic`.
- `--host`, `--port` (default `127.0.0.1:8080`) — web server bind.
- `--auth USER:PASS` (default off) — HTTP Basic auth for the web UI.
- `--embed-bridge` (default off) — run the bridge inside this process
  via the `pyrustuyabridge` bindings (single-process deploy). Refused
  at startup if another bridge already publishes on `--root`.
- `--bridge-state PATH` (default: `bridge-state.json` next to
  `--cloud`) — embedded bridge's device state file. **Only meaningful
  with `--embed-bridge`.**
- `--bridge-config PATH` (default off) — JSON config file for the
  embedded bridge. Same format as `rustuya-bridge --config`: existing
  file is read and merged (manager flags still win), missing file is
  auto-created from the merged settings. Lets you set custom topics /
  MQTT auth / scanner options without re-exposing every bridge flag
  here. **Only meaningful with `--embed-bridge`** — ignored otherwise.

### Run as a service (systemd, user-level, no sudo)

```bash
mkdir -p ~/.config/systemd/user ~/.local/share/rustuya-manager
cp examples/rustuya-manager.service ~/.config/systemd/user/
# edit the file — change --auth, --broker, --root to match your setup
systemctl --user daemon-reload
systemctl --user enable --now rustuya-manager
journalctl --user -u rustuya-manager -f         # follow logs
```

To keep the service running after logout (one-time, the only sudo step):
```bash
sudo loginctl enable-linger $USER
```

### Update

```bash
pipx upgrade rustuya-manager
systemctl --user restart rustuya-manager
```

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## License
MIT
