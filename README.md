# Rustuya Manager (AI-Assisted)

A management tool for rustuya-bridge to ensure efficient synchronization of Tuya devices.

## Overview

Rustuya Manager is a management tool developed with AI assistance that provides a centralized dashboard and interactive CLI to synchronize device information between the Tuya Cloud (via `tuyadevices.json`) and an active Rustuya Bridge. 

> [!TIP]
> Use **[tuyawizard](https://github.com/3735943886/tuyawizard)** to easily generate the latest `tuyadevices.json` from your Tuya Cloud account.

## Key Features

- AI-Assisted Implementation: Developed with the help of AI for robust data consistency and precise synchronization.
- Status Dashboard: Categorizes devices by status:
    - Synced: Data matches perfectly between cloud and bridge.
    - Key Mismatch: Device encryption keys updated in the cloud.
    - Missing (New): New devices found in cloud not yet added to bridge.
    - Orphaned: Devices removed from cloud but still present in bridge.
- Automatic Topic Resolution: Dynamically resolves MQTT topics based on your bridge configuration (config.json).
- Flexible UI Modes: Supports a visually enhanced experience with the `rich` library, or a clean Plain Text mode if dependencies are missing.
- Selective Sync: Allows manual or automatic updating of specific device categories.

## Quick Start

### 1. Prerequisites
- Python 3.9 or later
- Reachable MQTT Broker

### 2. Installation
Install the necessary MQTT client:
```bash
pip install paho-mqtt
```
Optional: Install 'rich' for an enhanced UI:
```bash
pip install rich
```

### 3. Usage
Execute the manager script in your root directory:
```bash
python3 rustuya_manager.py
```

Advanced Usage:
```bash
python3 rustuya_manager.py --config ./config.json --cloud ./tuyadevices.json --broker 192.168.1.100
```

## Configuration

The manager integrates directly with your existing **rustuya-bridge** configuration. It uses the exact same `config.json` structure and automatically extracts:

- mqtt_broker: Connection address and port.
- mqtt_root_topic: Base topic for the bridge.
- mqtt_command_topic: Template for bridge API commands.
- mqtt_event_topic: Template for device events.
- mqtt_message_topic: Template for bridge responses.

If config.json is not present, the tool will use default settings (localhost, root: rustuya) and display a warning.

## Development Note
This tool was built with the assistance of AI.

## License
MIT
