# Rustuya Manager

A management tool for [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) that diffs Tuya Cloud devices against the running bridge and syncs add / remove / update operations. Includes a web UI with built-in Tuya Cloud login.

![rustuya-manager web UI](docs/screenshots/main-annotated.png)
<sub>Desktop view — sync categories highlighted with their actions; the header's <b>☰ Menu</b> holds add device, cloud login, <b>📡 Scan</b>, theme, refresh, and <b>🔧 Reconfigure bridge</b>. Each row also carries a live-status dot and per-device ✎ edit / 🗑 remove / ↻ query-status icons.</sub>

<img src="docs/screenshots/main-mobile.png" alt="Mobile layout" width="280">
<br><sub>Mobile view — layout adapts to narrow viewports.</sub>

<sub>Other views: [unannotated](docs/screenshots/main-light.png) · [dark](docs/screenshots/main-dark.png) · [bulk sync](docs/screenshots/sync-modal.png)</sub>

## Key Features

- **Status dashboard** — Missing / Orphaned / Mismatched / Synced categories by diffing the Tuya Cloud device list against the bridge's live state.
- **Built-in Tuya Cloud login** — fetch the device list straight from the web UI; no external tooling needed. A `tuyadevices.json` upload / drop-zone is still available for offline workflows.
- **No separate config** — picks up the bridge's topic and payload templates from its retained `bridge/config`.
- **Live updates over MQTT** — DPS values stream into the UI in real time.
- **Web UI** — single-page UI with search, sort, sub-device tree, per-device add / edit / remove and bulk-sync.

## Usage

Start with `--web` (the Docker image does this by default). The
dashboard loads every device known to either side and categorizes
it by how the bridge's view compares to the Tuya Cloud-of-record
— uploaded as `tuyadevices.json` or pulled in-app via the header **☰ Menu → ☁ Fetch from cloud**:

- 🟦 **Missing** — in cloud, not yet on the bridge. Click **Add** on
  the card to publish it; the bridge picks it up and starts polling.
- 🟥 **Orphan** — on the bridge, not in cloud (or dropped from cloud
  since last sync). Click **🗑** to remove it from the bridge.
- 🟨 **Mismatch** — in both, but a field drifted (IP / key / version
  differ). Click **Update** to push the cloud values; expand the row
  to see exactly which fields are out of sync.
- 🟩 **Synced** — in both, fields match. No action needed.

Every per-card action has a bulk path — the buttons above the list
(**Add missing** / **Remove orphan** / **Update mismatch** / **Apply
all**) open a modal showing the full plan, let individual rows be
unchecked to skip, then run them sequentially with per-row status.

Click any row to expand it: live DP values stream in via MQTT, plus
the bridge's last error/status message and the resolved IP / key /
version. The pencil ✎ opens an editor that re-publishes the device
to the bridge with the modified fields (the cloud-of-record JSON is
unchanged); the trash 🗑 removes the device from the bridge.

### Refreshing from Tuya Cloud

The header **☰ Menu → ☁ Fetch from cloud** opens the in-app login wizard. Sign in
once via QR with the Smart Life or Tuya Smart app — credentials are
cached in `tuyacreds.json`, so subsequent re-fetches skip the scan and
go straight to the device list.

<img src="docs/screenshots/wizard-modal.png" alt="Tuya Cloud login modal" width="420">

**Scan device IPs after fetch** (off by default) decides what the
manager writes into the bridge record:

- **Off (default)** — devices ship to the bridge with no IP. The
  bridge runs its own LAN scan at runtime and catches DHCP IP
  changes automatically. Recommended unless every Tuya device on the
  LAN has a pinned address.
- **On** — best performance: the bridge **never scans the LAN** and
  every reconnect goes straight to the recorded address. Only useful
  when every device has a pinned IP (manual static or DHCP
  reservation on the router). On DHCP networks where leases rotate
  the bridge ends up retrying stale addresses; the header **☰ Menu →
  📡 Scan LAN** (or a fresh cloud re-fetch) recovers visibility.

The toggle state is persisted per browser.

**📡 Scan LAN** (header **☰ Menu**) asks the bridge for a one-shot LAN
scan. Any device registered with an explicit (non-auto) IP that has drifted
surfaces as `ERR_STATE 906` in the MSG line, so the right device can
be fixed at the router.

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
                --web --port 8373 --auth admin:CHANGE_ME
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
- `--host`, `--port` (default `127.0.0.1:8373`) — web server bind.
- `--auth USER:PASS` (default off) — HTTP Basic auth for the web UI.
- `--embed-bridge` (default off) — run the bridge inside this process
  via the `pyrustuyabridge` bindings (single-process deploy). Refused
  at startup if another bridge already publishes on `--root`.
- `--bridge-state PATH` (default: `rustuya.json` in the same
  directory as `--cloud`, matching the standalone bridge's filename) —
  embedded bridge's device state file. **Only meaningful with
  `--embed-bridge`.**
- `--bridge-config PATH` (default off) — JSON config for the embedded
  bridge, same format as `rustuya-bridge --config` (read and merged;
  auto-created if missing). Sets custom topics / MQTT auth / scanner
  options without re-exposing every bridge flag here. **Only meaningful
  with `--embed-bridge`.** For the three fields the manager and bridge
  share (`mqtt_broker`, `mqtt_root_topic`, `state_file`) the manager
  adopts the config's values as its own defaults; a CLI flag still wins.

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

## Docker

Single-container deploy with the bridge bundled in — aimed at HA OS,
unraid, CasaOS and similar container-first setups (distinct from the
pipx + systemd track above, which keeps `rustuya-bridge` as a separate
service).

```bash
docker run -d \
  --name rustuya-manager \
  --network host \
  --restart unless-stopped \
  -e AUTH=admin:CHANGE_ME \
  -v rustuya-manager-data:/data \
  3735943886/rustuya-manager:latest
```

The image runs `rustuya-manager --web --embed-bridge`, so manager and
bridge share one process and the only external dependency is an MQTT
broker. Key points:

- **`--network host` is required** — the embedded bridge discovers Tuya
  devices with UDP broadcasts (ports 6666/6667), which Docker's default
  bridge network drops, so devices are never seen.
- **Broker** defaults to `mqtt://localhost:1883` (host-local mosquitto
  under `--network host`). For a remote broker, set `mqtt_broker` in
  `/data/config.json` — the embedded bridge auto-creates that file and
  treats it as the single source of truth.
- **Already running a separate bridge?** Pass `-e EMBED_BRIDGE=0` so the
  container doesn't spawn a second one that double-publishes on the same
  topics.
- **`--restart unless-stopped`** lets Docker recover the manager — and
  with it a fresh embedded bridge — after the in-process supervisor hits
  its restart rate limit.

Environment variables (defaults shown; all optional unless noted):

| Variable | Default | Maps to |
|---|---|---|
| `HOST` | `0.0.0.0` | `--host` |
| `PORT` | `8373` | `--port` |
| `BROKER` | *(unset → bridge-config, then `mqtt://localhost:1883`)* | `--broker` |
| `ROOT` | *(unset → bridge-config, then `rustuya`)* | `--root` |
| `AUTH` | *(off)* | `--auth USER:PASS` |
| `CLOUD` | `/data/tuyadevices.json` | `--cloud` |
| `PLUGIN_DIR` | `/data/plugins` | `--plugin-dir` |
| `BRIDGE_CONFIG` | `/data/config.json` | `--bridge-config` |
| `BRIDGE_STATE` | *(unset → bridge-config `state_file`, then `/data/rustuya.json`)* | `--bridge-state` |
| `PUID` | `1000` | UID the app runs as |
| `PGID` | `1000` | GID the app runs as |
| `EMBED_BRIDGE` | `1` | `--embed-bridge` (set `0` when an external bridge is already on the broker) |

All persistent state lives under `/data` — cloud cache
(`tuyadevices.json`), wizard credentials (`tuyacreds.json`), bridge
config (`config.json`) and bridge state (`rustuya.json`) — so the volume
is the only backup target. Pass an empty value to disable an optional
flag (e.g. `-e BRIDGE_CONFIG=`).

For **bind-mounted** host directories (`-v /host/path:/data`), pass
`PUID`/`PGID` so the in-container user can write to them:

```bash
-e PUID=$(id -u) -e PGID=$(id -g)
```

The entrypoint renumbers its internal `manager` user to that UID/GID and
`chown`s `/data` on startup. Named volumes need none of this — Docker
handles ownership.

### Plugins

A plugin can add a UI tab, REST routes, an MQTT subscription, and its
own slice of state. They arrive three ways:

1. **From the in-UI catalog** (recommended) — the **Manage plugins** (🧩)
   item in the ☰ menu lists a curated catalog you install from with one
   click, no shell or `pip`.
2. **As a pip-installed package** under the `rustuya_manager.plugins`
   entry-point group.
3. **Hand-dropped** into the plugin directory (handy for development or
   Docker, where you'd otherwise rebuild the image).

All three run **in-process with no sandbox**, so the trust anchor is the
same in every case: only install plugins you trust. The catalog is
curated for exactly this reason — there is no arbitrary-URL field.

#### Installing from the catalog

**Manage plugins** (☰ → 🧩) opens a modal listing each catalog plugin
with its install state and actions:

- **Install** downloads the plugin into the managed plugin directory,
  verifies its checksum, and wires it up **live** — the new tab appears
  with no restart.
- **Update**, **Enable/Disable**, and **Uninstall** act on an installed
  plugin. These need a manager restart to take effect (already-loaded
  code can't be swapped or unloaded at runtime), so the modal offers a
  **Restart now** button when an action requires it.

Installs need a writable plugin directory (see below). Under Docker that
means a mounted `PLUGIN_DIR`.

#### The plugin directory

The managed plugin directory is where the catalog installs plugins and
where you can also hand-drop your own. It defaults to a `plugins/` folder
next to the cloud file; override it with `--plugin-dir DIR` (env
`RUSTUYA_MANAGER_PLUGIN_DIR`). Under Docker the image defaults
`PLUGIN_DIR=/data/plugins` — mount a folder there so installs persist.

To hand-drop a plugin, place a package (a directory with `__init__.py`
exposing `register(ctx)`) or a single `*.py` file in that directory:

```
<plugin-dir>/
  rustuya_hello/        # package plugin
    __init__.py         #   defines register(ctx)
    static/             #   its UI assets (served automatically)
  quicktweak.py         # single-file plugin: just register(ctx)
```

One caveat for hand-dropped plugins: they **can't install their own
dependencies** (they get the standard library plus what the manager
already provides — for anything heavier, install it as an entry-point
package or via the catalog instead). See
[`examples/hello_plugin`](examples/hello_plugin) for a complete plugin.

#### Loading without a restart

Two more ☰-menu items handle reloads for hand-dropped plugins:

- **Load new plugins** (📂) — scans the plugin dir and loads any *newly
  added* plugin live, no restart. Add-only: it can't pick up edits to an
  already-loaded plugin or unload one (live routes/mounts can't be
  cleanly removed). Catalog installs already do this for you.
- **Restart manager** (♻) — restarts the manager process in place (same
  PID, via re-exec). This is the full reload: it picks up edited plugin
  code, drops removed/disabled plugins, and respawns an embedded bridge —
  lighter than a container restart and works outside Docker too. The UI
  reconnects automatically; an embedded bridge briefly disconnects its
  devices.

## License
MIT
