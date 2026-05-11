# Rustuya Manager

A management tool for [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) that synchronizes Tuya devices between the Tuya Cloud (via `tuyadevices.json`) and the running bridge. Ships with both a CLI mode and a web UI.

> [!TIP]
> Use **[tuyawizard](https://github.com/3735943886/tuyawizard)** to generate `tuyadevices.json` from your Tuya Cloud account.

## Key Features

- **Status dashboard** — categorizes devices as Synced / Mismatched / Missing / Orphaned by diffing your cloud JSON against the bridge's live state.
- **Identical MQTT semantics** — topic and payload templating is delegated to [`pyrustuyabridge`](https://pypi.org/project/pyrustuyabridge/), the official Python binding to the bridge's own Rust functions. The manager interprets any custom topic/payload templates byte-identically to the bridge.
- **Self-configuring** — reads the bridge's retained `{root}/bridge/config` snapshot at startup, so no separate `config.json` is required.
- **Live updates over MQTT** — subscribes to event/message wildcards, streams DPS values to the UI in real time.
- **Resilient transport** — re-subscribes runtime topics after broker reconnects, bounded auto-reconnect backoff, CONNACK and subscribe failures logged.
- **Web UI** — single-page, no build pipeline (Tailwind via CDN + vanilla JS). Drag-and-drop cloud-JSON upload, search, sort, sub-device tree, live event stream, per-device actions.
- **CLI mode** — bootstrap + diff + event stream printed to stdout for SSH / log-shipping workflows.

## Quick Start

### Prerequisites
- Python 3.9 or later
- A running [rustuya-bridge](https://github.com/3735943886/rustuya-bridge) and reachable MQTT broker

### Install

```bash
git clone https://github.com/3735943886/rustuya-manager
cd rustuya-manager
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

The package pulls `pyrustuyabridge` from PyPI automatically; no Rust toolchain or maturin step required for users.

### Run

CLI mode (event stream + diff to stdout):
```bash
python3 rustuya-manager.py --broker mqtt://localhost:1883 --root rustuya
```

Web UI mode (open `http://localhost:8080`):
```bash
python3 rustuya-manager.py --broker mqtt://localhost:1883 --root rustuya --web --port 8080
```

Common flags:
- `--cloud PATH` — Tuya devices JSON (default `tuyadevices.json`). If the file is missing the UI shows a drop-zone for upload; uploads are persisted to this path.
- `--broker URL` — `mqtt://[user:pass@]host:port` (default `mqtt://localhost:1883`).
- `--root TOPIC` — must match the running bridge's `--mqtt-root-topic` (default `rustuya`).
- `--web --host HOST --port PORT` — start the FastAPI web server.

## How the manager stays in sync with the bridge

Rather than re-implementing the bridge's MQTT templating in Python (which was the source of the previously abandoned web app's bugs), the manager imports the bridge's own templating helpers via the `pyrustuyabridge` Python binding:

- `tpl_to_wildcard(template, root)` — converts a topic template to its MQTT subscription wildcard.
- `match_topic(topic, template)` — reverse-parses an incoming topic into a `{var: value}` map.
- `parse_payload(payload, vars)` — applies the bridge's payload-template parsing identically to how the bridge would handle it.
- `render_template(template, vars)` — forward substitution for building concrete command topics.

This is why any topic or payload template the bridge supports works in the manager without per-format glue code.

## Development

For working on the binding alongside the manager (uncommon — most users should just `pip install` the published wheel):

```bash
git clone https://github.com/3735943886/rustuya-bridge ../rustuya-bridge
. .venv/bin/activate
pip install maturin
cd ../rustuya-bridge/python && maturin develop && cd -
```

Run the test suite:
```bash
pytest tests/
```

## License
MIT
