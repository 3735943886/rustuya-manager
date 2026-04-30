# Rustuya Manager Web

A modern, real-time web management dashboard for **[rustuya-bridge](https://github.com/3735943886/rustuya-bridge)**, designed to ensure efficient synchronization and 24/7 monitoring of Tuya devices.

## Overview

Rustuya Manager Web provides a centralized, interactive dashboard to synchronize device information between the Tuya Cloud and the Rustuya Bridge engine. Built with **FastAPI** and **WebSockets**, it offers a responsive interface for managing your local smart home infrastructure with ease. 

It now seamlessly integrates the `rustuya-bridge` engine internally via native bindings, making deployment a single-container breeze!

> [!TIP]
> The built-in **Tuya Wizard** allows you to log in via QR code and fetch your latest device configurations directly from the cloud without manual JSON editing.

## Key Features

- **Built-in Bridge Engine**: Runs the Rust-powered bridge internally; no need to manage separate bridge containers!
- **Real-Time Dashboard**: Monitor device connectivity, live DPS (Data Point) values, and error logs via persistent, non-blocking WebSocket connections.
- **Granular Sync Status**: Automatically detects discrepancies between cloud and bridge:
    - <span style="color: #fb7185">**Missing**</span>: New devices found in cloud not yet added to bridge.
    - <span style="color: #fbbf24">**Mismatch**</span>: Existing devices with outdated encryption keys or metadata.
    - <span style="color: #94a3b8">**Orphaned**</span>: Devices present in bridge but removed from cloud.
- **Tuya Wizard**: Seamless QR code login flow to refresh `tuyadevices.json` directly from the web UI.
- **24/7 Reliability**: Fully asynchronous file I/O and strict memory management designed for uninterrupted Docker deployments.

## Quick Start (Recommended)

The recommended way to run Rustuya Manager is via **Docker**. This ensures all dependencies (including Rust bindings) are isolated and managed perfectly.

### 1. Prerequisites
- Docker & Docker Compose
- Reachable MQTT Broker (e.g., Mosquitto)

### 2. Docker Compose Example
Create a `docker-compose.yml` file:

```yaml
version: '3.8'
services:
  rustuya-manager:
    image: ghcr.io/3735943886/rustuya-manager:latest
    container_name: rustuya-manager
    restart: unless-stopped
    network_mode: host
    ports:
      - "8373:8373"
    volumes:
      - ./data:/data
```

Run the container:
```bash
docker-compose up -d
```

### 3. Docker Run Command (Alternative)
```bash
docker run -d \
  --name rustuya-manager \
  --restart unless-stopped \
  --network host \
  -p 8373:8373 \
  -v $(pwd)/data:/data \
  ghcr.io/3735943886/rustuya-manager:latest
```

Open your browser and navigate to `http://localhost:8373`.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_BROKER` | Address of your MQTT broker | `localhost:1883` |
| `MQTT_USER` | MQTT username (optional) | `None` |
| `MQTT_PASSWORD` | MQTT password (optional) | `None` |
| `LOG_LEVEL` | Bridge logging level (`debug`, `info`, `warn`) | `info` |

## Manual Installation (Development)

If you wish to run without Docker for development:

1. Install **Python 3.11+** and **Rust**.
2. Clone the repository and install dependencies:
```bash
pip install -r requirements.txt
```
3. Run the application:
```bash
uvicorn web.app:app --host 0.0.0.0 --port 8373
```

## License
MIT
