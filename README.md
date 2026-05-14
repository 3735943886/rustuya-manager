# Rustuya Manager

A management tool for [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) that diffs Tuya Cloud devices against the running bridge and syncs add / remove / update operations. Ships with a web UI with built-in Tuya Cloud login.

## Key Features

- **Status dashboard** — Missing / Orphaned / Mismatched / Synced categories by diffing the Tuya Cloud device list against the bridge's live state.
- **Built-in Tuya Cloud login** — fetch the device list straight from the web UI; no external tooling needed. A `tuyadevices.json` upload / drop-zone is still available for offline workflows.
- **No separate config** — picks up the bridge's topic and payload templates from its retained `bridge/config`.
- **Live updates over MQTT** — DPS values stream into the UI in real time.
- **Web UI** — single-page UI with search, sort, sub-device tree, per-device add / edit / remove and bulk-sync.

## Quick Start

Requires Python 3.10+ and a running [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) reachable via MQTT.

### Install

**pipx (recommended)** — drops a `rustuya-manager` shim into `~/.local/bin/`, no activate step:
```bash
sudo apt install -y pipx                          # if not already
pipx ensurepath
pipx install rustuya-manager
```

**venv + pip** — alternative install without pipx:
```bash
python3 -m venv ~/.venvs/rustuya-manager
~/.venvs/rustuya-manager/bin/pip install rustuya-manager
~/.venvs/rustuya-manager/bin/rustuya-manager --help
```
Run it by full path, or activate the venv first (`source ~/.venvs/rustuya-manager/bin/activate`). The systemd unit in the next section assumes the pipx path — change `ExecStart` to `%h/.venvs/rustuya-manager/bin/rustuya-manager` for the venv install.

### Run

```bash
rustuya-manager --broker mqtt://localhost:1883 --root rustuya \
                --web --port 8080 --auth admin:CHANGE_ME
```
Then open the URL printed at startup. The default bind is `127.0.0.1` so the UI is reachable only from the same machine. To open it to the LAN add `--host 0.0.0.0` — pair with a real `--auth user:pass`.

Common flags:
- `--cloud PATH` (default `tuyadevices.json`) — Tuya devices JSON. If
  missing, the web UI offers an in-app Tuya Cloud login or a JSON
  drop-zone.
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
  file is read and merged, missing file is auto-created from the
  merged settings. Allows setting custom topics / MQTT auth / scanner
  options without re-exposing every bridge flag here. **Only meaningful
  with `--embed-bridge`** — ignored otherwise.

  Special handling for the two fields that the manager and the bridge
  *both* care about (`mqtt_broker`, `mqtt_root_topic`): when
  `--bridge-config` supplies them, the manager adopts them as its own
  defaults too, so they only need to be specified once. Precedence:
    1. CLI flag (`--broker`, `--root`)
    2. value from `--bridge-config`
    3. manager default (`mqtt://localhost:1883`, `rustuya`)

  If a CLI flag and the bridge-config value disagree, the CLI value
  overrides (the embedded bridge ends up with the same kwarg) and a
  warning is logged so the contradiction doesn't go unnoticed.

### Run as a service (systemd, user-level, no sudo)

```bash
mkdir -p ~/.config/systemd/user ~/.local/share/rustuya-manager
cp examples/rustuya-manager.service ~/.config/systemd/user/
# edit the file — change --auth, --broker, --root to match the local setup
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
pipx upgrade rustuya-manager                                   # pipx install
# or, for the venv install:
~/.venvs/rustuya-manager/bin/pip install -U rustuya-manager
systemctl --user restart rustuya-manager
```

## License
MIT
