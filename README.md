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

```bash
pip install git+https://github.com/3735943886/rustuya-manager
```

Run (CLI):
```bash
rustuya-manager --broker mqtt://localhost:1883 --root rustuya
```

Run (web UI on http://localhost:8080):
```bash
rustuya-manager --broker mqtt://localhost:1883 --root rustuya --web --port 8080
```

Common flags:
- `--cloud PATH` — Tuya devices JSON (default `tuyadevices.json`). If missing, the UI shows a drop-zone for upload.
- `--broker URL` — `mqtt://[user:pass@]host:port`.
- `--root TOPIC` — must match the bridge's `--mqtt-root-topic`.
- `--web --host HOST --port PORT` — start the web server.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## License
MIT
