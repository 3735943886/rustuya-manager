import asyncio
import json
import logging
import os
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path

import aiomqtt
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pyrustuyabridge import PyBridgeServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rustuya-manager")

# ---------------------------------------------------------------------------
# Constants & Paths
# ---------------------------------------------------------------------------
BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = Path(os.getenv("DATA_DIR", BASE_DIR.parent))
CLOUD_PATH = DATA_DIR / "tuyadevices.json"
CREDS_PATH = DATA_DIR / "tuyacreds.json"

CONFIG_DISCOVERY_TOPIC = "rustuya/bridge/config"
BRIDGE_ACTIONS = {"add", "remove", "status", "query", "get", "delete"}

# MQTT Backoff Constants
INITIAL_RETRY_DELAY_SECS = 10
MAX_RETRY_DELAY_SECS = 1280

ENV_TO_KWARG = {
    "MQTT_BROKER": "mqtt_broker",
    "STATE_FILE": "state_file",
    "CONFIG": "config_path",
    "LOG_LEVEL": "log_level",
    "MQTT_USER": "mqtt_user",
    "MQTT_PASSWORD": "mqtt_password",
    "MQTT_ROOT_TOPIC": "mqtt_root_topic",
    "MQTT_COMMAND_TOPIC": "mqtt_command_topic",
    "MQTT_EVENT_TOPIC": "mqtt_event_topic",
    "MQTT_CLIENT_ID": "mqtt_client_id",
    "MQTT_MESSAGE_TOPIC": "mqtt_message_topic",
    "MQTT_PAYLOAD_TEMPLATE": "mqtt_payload_template",
    "MQTT_SCANNER_TOPIC": "mqtt_scanner_topic"
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    mqtt_broker:         str       = "localhost"
    mqtt_port:           int       = 1883
    mqtt_root_topic:     str       = "rustuya"
    mqtt_user:           str | None = None
    mqtt_password:       str | None = None
    mqtt_command_topic:  str       = "{root}/command"
    mqtt_event_topic:    str       = "{root}/event/{type}/{id}"
    mqtt_message_topic:  str       = "{root}/{level}/{id}"
    mqtt_scanner_topic:  str       = "{root}/scanner"

    def update_from_dict(self, data: dict) -> None:
        if broker := data.get("mqtt_broker"):
            if "://" in broker:
                broker = broker.split("://")[-1]
            host, *rest = broker.split(":")
            self.mqtt_broker = host
            if rest:
                self.mqtt_port = int(rest[0])
                
        for f in fields(self):
            name = f.name
            if name not in ("mqtt_broker", "mqtt_port") and name in data:
                setattr(self, name, data[name])

    def format(self, template: str, **kwargs) -> str:
        """Universal topic/payload formatter with global root and common defaults."""
        res = template.replace("{root}", self.mqtt_root_topic)
        ctx = {
            "id": "", "name": "", "cid": "", "level": "", "type": "",
            "dp": "", "action": "", "timestamp": int(time.time()),
            "value": kwargs.get("value") or kwargs.get("dps_str", ""),
            "dps":   kwargs.get("dps_str", ""),
            **kwargs
        }
        for k, v in ctx.items():
            key = f"{{{k}}}"
            if key in res:
                val = str(v)
                if not val: res = res.replace(f"/{key}", "")
                res = res.replace(key, val)
        return res

    def resolve_command_topic(self, action: str, device_id: str | None = None) -> str:
        """Resolve command topic using standardized formatter."""
        return self.format(self.mqtt_command_topic, action=action, id=device_id)


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    config:               AppConfig         = field(default_factory=AppConfig)
    devices_map:          dict              = field(default_factory=dict)
    websocket_connections: set[WebSocket]   = field(default_factory=set)
    background_tasks:     set[asyncio.Task] = field(default_factory=set)
    mqtt_client:          aiomqtt.Client | None = None
    mqtt_connected:       bool              = False


state = AppState()


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------
async def _send_raw(ws: WebSocket, payload: str) -> None:
    try:
        await ws.send_text(payload)
    except Exception:
        state.websocket_connections.discard(ws)


async def broadcast(message: dict) -> None:
    if state.websocket_connections:
        try:
            payload = json.dumps(message)
            await asyncio.gather(*(_send_raw(ws, payload) for ws in list(state.websocket_connections)))
        except Exception as e:
            logger.debug("Broadcast failed (expected during shutdown): %s", e)


async def send_to(ws: WebSocket, message: dict) -> None:
    await _send_raw(ws, json.dumps(message))


# ---------------------------------------------------------------------------
# MQTT message helpers
# ---------------------------------------------------------------------------
def extract_devices(payload: dict) -> dict | None:
    """Extract device list/dict from various payload shapes."""
    devs = payload.get("devices")
    if devs is None and isinstance(data := payload.get("data"), dict):
        devs = data.get("devices")
            
    if devs is None:
        return None
        
    if isinstance(devs, list):
        return {d["id"]: d for d in devs if isinstance(d, dict) and "id" in d}
    if isinstance(devs, dict):
        return devs
    return {}


def _match_template(template: str | None, root: str, topic: str) -> dict[str, str] | None:
    """Match topic against template segment-by-segment and capture {vars}."""
    if not template: return None
    tmpl_parts  = template.replace("{root}", root).split("/")
    topic_parts = topic.split("/")
    
    # MQTT wildcard support (only /# at the end)
    if tmpl_parts[-1] == "#":
        tmpl_parts = tmpl_parts[:-1]
        if len(topic_parts) < len(tmpl_parts): return None
        topic_parts = topic_parts[:len(tmpl_parts)]
    elif len(topic_parts) != len(tmpl_parts):
        return None
            
    captured = {}
    for tmpl_seg, topic_seg in zip(tmpl_parts, topic_parts):
        if tmpl_seg.startswith("{") and tmpl_seg.endswith("}"):
            captured[tmpl_seg[1:-1]] = topic_seg
        elif tmpl_seg != topic_seg:
            return None
    return captured


def classify_mqtt_topic(topic: str) -> tuple[str, dict[str, str]]:
    """Strict template-based classification: 'event'|'response'|'error'|'scanner'|'command'."""
    cfg = state.config
    
    # Rule-based matching in order of priority
    rules = [
        (cfg.mqtt_event_topic,   "event"),
        (cfg.mqtt_message_topic, "message"),
        (cfg.mqtt_scanner_topic, "scanner"),
        (cfg.mqtt_command_topic, "command"),
    ]
    
    for template, base_type in rules:
        m = _match_template(template, cfg.mqtt_root_topic, topic)
        if m is None: continue
        
        # Resolve specific subtype
        sub = m.get("level") or m.get("type") or base_type
        if sub in ("response", "error"): return sub, m
        if base_type == "event":         return "event", m
        return ("response" if base_type == "message" else base_type), m
            
    return "unknown", {}


def _handle_response(did: str, action: str | None, payload: dict) -> tuple[bool, dict | None, bool]:
    devs = extract_devices(payload)
    if devs is not None:
        state.devices_map = devs
        return True, dict(state.devices_map), False

    status = str(payload.get("status", "")).lower()
    is_success = status in ("success", "ok") or payload.get("status") is True

    if action == "remove" and is_success:
        state.devices_map.pop(did, None)
        logger.info("Removed device %s from local state", did)
        return True, dict(state.devices_map), True

    if action == "add" and is_success:
        logger.info("Device add confirmed, refreshing status")
        return False, None, True

    return False, None, False


def _handle_event_error(
    did: str, topic_type: str, action: str | None, payload: object, captured_vars: dict[str, str]
) -> tuple[bool, dict | None, bool]:
    if action:  # bridge command echo, not a device update
        return False, None, False

    filtered = {"status": "online"} if topic_type == "event" else {}
    if "dp" in captured_vars:
        # Per-DP mode: topic contains {dp} and potentially {value}
        dp = captured_vars["dp"]
        val = payload if payload is not None and payload != "" else captured_vars.get("value")
        filtered = {dp: val}
    elif isinstance(payload, dict):
        # Bulk mode: payload contains dict of DPs
        JUNK = {"errorCode", "errorMsg", "payloadStr", "id", "action"}
        filtered = {k: v for k, v in payload.items() if k not in JUNK}
        if topic_type == "error" and "errorCode" in payload:
            filtered["status"] = "online" if payload["errorCode"] == 0 else str(payload["errorCode"])
    else:
        return False, None, False

    if did in state.devices_map:
        state.devices_map[did].update(filtered)
    else:
        logger.info("Discovered device via %s: %s (%s)", topic_type, filtered.get("name", "?"), did)
        state.devices_map[did] = {**filtered, "id": did}

    return True, dict(state.devices_map), False


def handle_mqtt_message(
    topic: str, payload: object, topic_type: str, captured_vars: dict[str, str]
) -> tuple[bool, dict | None, bool]:
    """
    Update devices_map from an MQTT message.
    Returns: (devices_updated, snapshot | None, should_request_status)
    """
    did = captured_vars.get("id")
    action = None
    if isinstance(payload, dict):
        action = payload.get("action")
        # Ensure payload has id for frontend compatibility
        if not payload.get("id") and did:
            payload["id"] = did
        did = payload.get("id") or did

    if not did:
        return False, None, False

    if topic_type == "response" and isinstance(payload, dict):
        return _handle_response(did, action, payload)

    if topic_type in ("error", "event"):
        return _handle_event_error(did, topic_type, action, payload, captured_vars)

    return False, None, False


def decode_payload(message) -> object:
    payload = message.payload.decode()
    try:
        return json.loads(payload)
    except Exception:
        return payload


# ---------------------------------------------------------------------------
# MQTT listener
# ---------------------------------------------------------------------------
async def mqtt_listener() -> None:
    reconnect_delay = INITIAL_RETRY_DELAY_SECS

    while True:
        cfg = state.config
        try:
            async with aiomqtt.Client(
                hostname=cfg.mqtt_broker, 
                port=cfg.mqtt_port,
                username=cfg.mqtt_user,
                password=cfg.mqtt_password
            ) as client:
                state.mqtt_client    = client
                state.mqtt_connected = True
                reconnect_delay      = INITIAL_RETRY_DELAY_SECS  # Reset delay on successful connection
                logger.info("MQTT connected: %s:%d", cfg.mqtt_broker, cfg.mqtt_port)
                await broadcast({"type": "mqtt_status", "connected": True})

                await client.subscribe(f"{cfg.mqtt_root_topic}/#")

                async for message in client.messages:
                    topic = str(message.topic)
                    payload = decode_payload(message)

                    topic_type, captured_vars          = classify_mqtt_topic(topic)
                    if topic_type in ("command", "unknown"):
                        continue  # command: our own echo; unknown: unrecognised topic

                    devices_updated, snapshot, refresh = handle_mqtt_message(topic, payload, topic_type, captured_vars)

                    if refresh:
                        await client.publish(
                            cfg.resolve_command_topic("status"),
                            json.dumps({"action": "status"}),
                        )

                    await broadcast({
                        "type":            "mqtt",
                        "topic_type":      topic_type,
                        "topic":           topic,
                        "payload":         payload,
                        "id":              captured_vars.get("id"),
                        "devices_updated": devices_updated,
                        "devices":         snapshot,
                    })

        except asyncio.CancelledError:
            raise
        except (aiomqtt.MqttError, Exception) as e:
            level = logger.warning if isinstance(e, aiomqtt.MqttError) else logger.error
            level("%s: %s — retrying in %ds", type(e).__name__, e, reconnect_delay)
            
            state.mqtt_client    = None
            state.mqtt_connected = False
            await broadcast({"type": "mqtt_status", "connected": False})
            
            try:
                await asyncio.sleep(reconnect_delay)
            except asyncio.CancelledError:
                raise
            reconnect_delay = min(reconnect_delay * 2, MAX_RETRY_DELAY_SECS)


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
async def run_wizard(user_code: str) -> None:
    from tuyawizard.wizard import TuyaWizard, postprocess_devices

    loop = asyncio.get_running_loop()

    async def wizard_update(step: str, *, running: bool = True,
                            url: str | None = None, error: str | None = None, **extra) -> None:
        status: dict = {"running": running, "step": step, "url": url}
        if error is not None:
            status["error"] = error
        await broadcast({"type": "wizard", "status": status, **extra})

    def qr_callback(url: str | None) -> None:
        step = "Waiting for app scan..." if url else "Fetching devices..."
        asyncio.run_coroutine_threadsafe(wizard_update(step, url=url), loop)

    try:
        await wizard_update("Starting API Login...")
        tuya = TuyaWizard(info_file=str(CREDS_PATH))
        await asyncio.to_thread(tuya.login_auto, user_code=user_code, qr_callback=qr_callback)

        await wizard_update("Fetching devices...")
        tuyadevices = await asyncio.to_thread(tuya.fetch_devices)

        await wizard_update("Matching sub-devices and scanning IPs...")
        await asyncio.to_thread(postprocess_devices, tuyadevices, "all")

        await wizard_update("Saving devices...")
        await asyncio.to_thread(
            CLOUD_PATH.write_text, 
            json.dumps(tuyadevices, indent=4, ensure_ascii=False), 
            encoding="utf-8"
        )

        init_data = await load_init_data()
        await wizard_update("Done!", running=False, **init_data)

    except Exception as e:
        logger.error("Wizard error: %s", e)
        await wizard_update("Wizard failed.", running=False, error=str(e))


# ---------------------------------------------------------------------------
# Init data / WS command
# ---------------------------------------------------------------------------
async def _load_json(path: Path, default=None):
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return json.loads(content)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error("Error reading %s: %s", path, e)
        return default


async def load_init_data() -> dict:
    raw = await _load_json(CLOUD_PATH, [])
    items = raw if isinstance(raw, list) else list(raw.values())
    cloud_devices = {d["id"]: d for d in items if isinstance(d, dict) and "id" in d}
    creds = await _load_json(CREDS_PATH, {})
    user_code = creds.get("user_code", "") if isinstance(creds, dict) else ""
    return {"cloud_devices": cloud_devices, "user_code": user_code}


async def handle_ws_command(cmd: dict, websocket: WebSocket) -> None:
    action  = cmd.get("action", "")
    payload = cmd.get("payload", {})

    if action in BRIDGE_ACTIONS:
        action = "remove" if action == "delete" else action
        payload["action"] = action
        if state.mqtt_client:
            pub_topic = state.config.resolve_command_topic(action, device_id=payload.get("id"))
            await state.mqtt_client.publish(pub_topic, json.dumps(payload))
        else:
            await send_to(websocket, {
                "type":    "bridge_response",
                "level":   "error",
                "message": "MQTT broker not connected. Command not sent.",
            })
        return

    if action == "wizard_start":
        task = asyncio.create_task(run_wizard(payload.get("user_code", "")))
        state.background_tasks.add(task)
        task.add_done_callback(state.background_tasks.discard)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
async def fetch_config_from_mqtt(
    cfg: AppConfig | None = None,
    *,
    stop: asyncio.Event | None = None,
) -> tuple[AppConfig, dict]:
    """Subscribe to the bridge config topic and return parsed AppConfig and raw data with retry."""
    cfg = cfg or AppConfig()
    reconnect_delay = INITIAL_RETRY_DELAY_SECS

    while True:
        if stop and stop.is_set():
            raise asyncio.CancelledError("Shutdown requested")
        try:
            logger.info("Fetching config from %s (via %s:%d)...", CONFIG_DISCOVERY_TOPIC, cfg.mqtt_broker, cfg.mqtt_port)
            async with aiomqtt.Client(
                hostname=cfg.mqtt_broker, 
                port=cfg.mqtt_port,
                username=cfg.mqtt_user,
                password=cfg.mqtt_password
            ) as client:
                await client.subscribe(CONFIG_DISCOVERY_TOPIC)
                async with asyncio.timeout(5.0):
                    async for message in client.messages:
                        if str(message.topic) == CONFIG_DISCOVERY_TOPIC:
                            data = json.loads(message.payload.decode())
                            logger.info("Config received: %s", data)
                            cfg.update_from_dict(data)
                            return cfg, data
        except asyncio.CancelledError:
            raise
        except (aiomqtt.MqttError, asyncio.TimeoutError, Exception) as e:
            reason = "Timeout" if isinstance(e, asyncio.TimeoutError) else str(e)
            logger.warning("Config fetch failed (%s). Retrying in %ds...", reason, reconnect_delay)

            # Race between sleep and stop event so Ctrl+C wakes us immediately
            if stop:
                sleep_task = asyncio.ensure_future(asyncio.sleep(reconnect_delay))
                stop_task  = asyncio.ensure_future(stop.wait())
                try:
                    done, pending = await asyncio.wait(
                        [sleep_task, stop_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in pending:
                        t.cancel()
                if stop.is_set():
                    raise asyncio.CancelledError("Shutdown requested")
            else:
                try:
                    await asyncio.sleep(reconnect_delay)
                except asyncio.CancelledError:
                    raise

            reconnect_delay = min(reconnect_delay * 2, MAX_RETRY_DELAY_SECS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Determine config path
    config_path = Path(os.environ.get("CONFIG", DATA_DIR / "config.json"))
    
    # 2. Start with defaults for bridge
    bridge_kwargs = {
        "mqtt_broker": "localhost",
        "state_file": str(DATA_DIR / "rustuya.json"),
        "config_path": str(config_path),
        "log_level": "info",
        "no_signals": True,
    }
    
    # 3. Load from config.json if it exists
    if config_path.exists():
        try:
            file_config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(file_config, dict):
                bridge_kwargs.update(file_config)
                logger.info("Loaded bridge settings from %s", config_path)
        except Exception as e:
            logger.warning("Failed to read config file %s: %s", config_path, e)

    # 4. Override with environment variables (Priority: Env > File)
    env_overrides = {
        kwarg: os.environ[env] for env, kwarg in ENV_TO_KWARG.items() if env in os.environ
    }
    bridge_kwargs.update(env_overrides)
    
    # Handle special types from env
    if "MQTT_RETAIN" in os.environ:
        bridge_kwargs["mqtt_retain"] = os.environ["MQTT_RETAIN"].lower() in ("true", "1", "yes", "t")
        
    if "SAVE_DEBOUNCE_SECS" in os.environ:
        try:
            bridge_kwargs["save_debounce_secs"] = int(os.environ["SAVE_DEBOUNCE_SECS"])
        except ValueError:
            pass

    # 5. Initialize and start bridge
    bridge = PyBridgeServer(**bridge_kwargs)

    async def start_bridge():
        await bridge.start_async()

    bridge_task = asyncio.create_task(start_bridge())
    logger.info("Internal rustuya-bridge started with: %s", {k: v for k, v in bridge_kwargs.items() if "password" not in k})

    # 6. Prepare initial config for manager discovery
    init_cfg = AppConfig()
    init_cfg.update_from_dict(bridge_kwargs)

    # 7. Re-register Python SIGINT/SIGTERM handlers so Ctrl+C works even when
    #    the Rust bridge replaces the default signal handler during start_async().
    _stop = asyncio.Event()
    loop  = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT,  _stop.set)
        loop.add_signal_handler(signal.SIGTERM, _stop.set)
    except (NotImplementedError, OSError):
        pass  # Windows / some edge environments

    # 8. Wait for config to be published via MQTT and start listener
    task = None
    try:
        try:
            state.config, config_data = await fetch_config_from_mqtt(init_cfg, stop=_stop)
        finally:
            # Restore handlers so uvicorn can manage signals during normal operation
            try:
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            except (NotImplementedError, OSError):
                pass

        # Save config to file if it doesn't exist
        if not config_path.exists():
            logger.info("Saving received config to %s", config_path)
            await asyncio.to_thread(
                config_path.write_text,
                json.dumps(config_data, indent=4, ensure_ascii=False),
                encoding="utf-8"
            )

        task = asyncio.create_task(mqtt_listener())
        yield
    finally:
        # Shutdown
        async def cleanup():
            logger.info("Shutting down manager (cleanup phase 1/3)...")
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            logger.info("Shutting down manager (cleanup phase 2/3)...")
            bridge_task.cancel()
            try:
                # Give the bridge a moment to see the cancellation
                await asyncio.wait_for(bridge_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            
            logger.info("Shutting down manager (cleanup phase 3/3: closing bridge)...")
            try:
                # Explicitly call close to ensure MQTT disconnect and state saving
                await bridge.close()
                logger.info("Internal rustuya-bridge stopped.")
            except Exception as e:
                logger.error("Error during bridge close: %s", e)

        # Shield the cleanup task so it cannot be cancelled by Starlette/Uvicorn's exit signals.
        # This ensures we have a chance to finish MQTT flushing and state saving.
        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            logger.warning("Shutdown signal received during cleanup, waiting for completion...")
            try:
                # Still wait for the cleanup to finish, but with a hard timeout
                await asyncio.wait_for(cleanup_task, timeout=10.0)
            except Exception as e:
                logger.error("Cleanup failed to complete in time: %s", e)
        except Exception as e:
            logger.error("Unexpected error during cleanup: %s", e)


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
async def get_index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.websocket_connections.add(websocket)
    try:
        init_data = await load_init_data()
        await websocket.send_text(json.dumps({
            "type":           "init",
            "devices":        state.devices_map,
            "mqtt_connected": state.mqtt_connected,
            **init_data,
        }))
        async for raw in websocket.iter_text():
            await handle_ws_command(json.loads(raw), websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS error: %s", e)
    finally:
        state.websocket_connections.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    # Use PORT environment variable with default 8373
    port = int(os.getenv("PORT", 8373))
    uvicorn.run(app, host="0.0.0.0", port=port)

