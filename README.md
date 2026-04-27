# Rustuya Manager Web

A modern, real-time web management dashboard for **[rustuya-bridge](https://github.com/3735943886/rustuya-bridge)**, designed to ensure efficient synchronization and monitoring of Tuya devices.

## Overview

Rustuya Manager Web provides a centralized, interactive dashboard to synchronize device information between the Tuya Cloud and an active Rustuya Bridge. Built with **FastAPI** and **WebSockets**, it offers a responsive interface for managing your local smart home infrastructure with ease.

> [!TIP]
> The built-in **Tuya Wizard** allows you to log in via QR code and fetch your latest device configurations directly from the cloud without manual JSON editing.

## Key Features

- **Real-Time Dashboard**: Monitor device connectivity, live DPS (Data Point) values, and error logs via persistent WebSocket connections.
- **Granular Sync Status**: Automatically detects discrepancies between cloud and bridge:
    - <span style="color: #fb7185">**Missing**</span>: New devices found in cloud not yet added to bridge.
    - <span style="color: #fbbf24">**Mismatch**</span>: Existing devices with outdated encryption keys or metadata.
    - <span style="color: #94a3b8">**Orphaned**</span>: Devices present in bridge but removed from cloud.
- **Tuya Wizard**: Seamless QR code login flow to refresh `tuyadevices.json` directly from the web UI.
- **Topology View**: Visual tree representation to understand device-parent relationships (e.g., Zigbee/BLE sub-devices).
- **Auto Topic Resolution**: Dynamically adapts to your `config.json` MQTT topic templates.

## Quick Start

### 1. Prerequisites
- Python 3.9 or later
- Reachable MQTT Broker (e.g., Mosquitto)

### 2. Installation
Clone the repository and install the dependencies:
```bash
pip install -r requirements.txt
```

### 3. Usage
Run the FastAPI application:
```bash
uvicorn web.app:app --host 0.0.0.0 --port 8000
```
Open your browser and navigate to `http://localhost:8000`.

## Configuration

The manager integrates directly with your **rustuya-bridge** configuration. It reads `config.json` from the parent directory and automatically extracts:

- `mqtt_broker`: Connection address and port.
- `mqtt_root_topic`: Base topic for the bridge (default: `rustuya`).
- `mqtt_command_topic`: Template for bridge API commands.
- `mqtt_message_topic`: Template for bridge responses.

## License
MIT
