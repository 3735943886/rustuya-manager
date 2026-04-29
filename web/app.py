import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiomqtt
from pyrustuyabridge import PyBridgeServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rustuya-web")

import os
# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = Path(os.getenv("DATA_DIR", BASE_DIR.parent))
CLOUD_PATH = DATA_DIR / "tuyadevices.json"
CREDS_PATH = DATA_DIR / "tuyacreds.json"

CONFIG_DISCOVERY_TOPIC = "rustuya/bridge/config"
BRIDGE_ACTIONS = {"add", "remove", "status", "query", "get", "delete"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    mqtt_broker:         str       = "localhost"
    mqtt_port:           int       = 1883
    root_topic:          str       = "rustuya"
    mqtt_command_topic:  str       = "{root}/command"
    mqtt_event_topic:    str       = "{root}/event/{type}/{id}"
    mqtt_message_topic:  str       = "{root}/{level}/{id}"
    mqtt_scanner_topic:  str       = "{root}/scanner"

    def update_from_dict(self, data: dict) -> None:
        broker = data.get("mqtt_broker", "localhost:1883")
        if "://" in broker:
            broker = broker.split("://")[-1]
        host, *rest = broker.split(":")
        self.mqtt_broker        = host
        self.mqtt_port          = int(rest[0]) if rest else 1883
        self.root_topic         = data.get("mqtt_root_topic",    "rustuya")
        self.mqtt_command_topic = data.get("mqtt_command_topic", "{root}/command")
        self.mqtt_event_topic   = data.get("mqtt_event_topic",   "{root}/event/{type}/{id}")
        self.mqtt_message_topic = data.get("mqtt_message_topic", "{root}/{level}/{id}")
        self.mqtt_scanner_topic = data.get("mqtt_scanner_topic", "{root}/scanner")

    def format(self, template: str, **kwargs) -> str:
        """Universal topic/payload formatter with global root and common defaults."""
        res = template.replace("{root}", self.root_topic)
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
    mqtt_client:          aiomqtt.Client | None = None
    mqtt_connected:       bool              = False


state = AppState()


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------
async def broadcast(message: dict) -> None:
    if not state.websocket_connections:
        return
    payload = json.dumps(message)
    results = await asyncio.gather(
        *(ws.send_text(payload) for ws in state.websocket_connections),
        return_exceptions=True,
    )
    dead = {
        ws for ws, r in zip(list(state.websocket_connections), results)
        if isinstance(r, Exception)
    }
    state.websocket_connections -= dead


async def send_to(websocket: WebSocket, message: dict) -> None:
    try:
        await websocket.send_text(json.dumps(message))
    except Exception:
        state.websocket_connections.discard(websocket)


# ---------------------------------------------------------------------------
# MQTT message helpers
# ---------------------------------------------------------------------------
def extract_devices(payload: dict) -> dict | None:
    """Extract device list/dict from various payload shapes."""
    devs = payload.get("devices") or payload.get("data", {}).get("devices")
    if "devices" not in payload and not (
        isinstance(payload.get("data"), dict) and "devices" in payload["data"]
    ):
        return None
    if isinstance(devs, list):
        return {d["id"]: d for d in devs if "id" in d}
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
        m = _match_template(template, cfg.root_topic, topic)
        if m is None: continue
        
        # Resolve specific subtype
        sub = m.get("level") or m.get("type") or base_type
        if sub in ("response", "error"): return sub, m
        if base_type == "event":         return "event", m
        return ("response" if base_type == "message" else base_type), m
            
    return "unknown", {}


def handle_mqtt_message(
    topic: str, payload: object, topic_type: str, captured_vars: dict[str, str]
) -> tuple[bool, dict | None, bool]:
    """
    Update devices_map from an MQTT message.
    Returns: (devices_updated, snapshot | None, should_request_status)
    """
    did    = captured_vars.get("id")
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
        devs = extract_devices(payload)
        if devs is not None:
            state.devices_map = devs
            return True, dict(state.devices_map), False

        status     = str(payload.get("status", "")).lower()
        is_success = status in ("success", "ok") or payload.get("status") is True

        if action == "remove" and is_success:
            state.devices_map.pop(did, None)
            logger.info("Removed device %s from local state", did)
            return True, dict(state.devices_map), True

        if action == "add" and is_success:
            logger.info("Device add confirmed, refreshing status")
            return False, None, True

        return False, None, False

    if topic_type in ("error", "event"):
        if action:  # bridge command echo, not a device update
            return False, None, False

        filtered = {"status": "online"} if topic_type == "event" else {}
        if "dp" in captured_vars:
            # Per-DP mode: topic contains {dp} and potentially {value}
            dp = captured_vars["dp"]
            # Priority: payload > captured value segment
            val = payload if payload is not None and payload != "" else captured_vars.get("value")
            filtered = {dp: val}
        elif isinstance(payload, dict):
            # Bulk mode: payload contains dict of DPs
            JUNK = {"errorCode", "errorMsg", "payloadStr", "id", "action"}
            filtered = {k: v for k, v in payload.items() if k not in JUNK}
            if topic_type == "error" and "errorCode" in payload:
                filtered["status"] = "online" if payload["errorCode"] == 0 else str(payload["errorCode"])
        else:
            # Unknown payload format for event/error
            return False, None, False

        if did in state.devices_map:
            state.devices_map[did].update(filtered)
        else:
            logger.info("Discovered device via %s: %s (%s)", topic_type, filtered.get("name", "?"), did)
            state.devices_map[did] = {**filtered, "id": did}

        return True, dict(state.devices_map), False

    return False, None, False


# ---------------------------------------------------------------------------
# MQTT listener
# ---------------------------------------------------------------------------
async def mqtt_listener() -> None:
    cfg             = state.config
    reconnect_delay = 5

    while True:
        try:
            async with aiomqtt.Client(hostname=cfg.mqtt_broker, port=cfg.mqtt_port) as client:
                state.mqtt_client    = client
                state.mqtt_connected = True
                logger.info("MQTT connected: %s:%d", cfg.mqtt_broker, cfg.mqtt_port)
                await broadcast({"type": "mqtt_status", "connected": True})

                await client.subscribe(f"{cfg.root_topic}/#")

                async for message in client.messages:
                    topic = str(message.topic)
                    try:
                        payload = json.loads(message.payload.decode())
                    except Exception:
                        payload = message.payload.decode()

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

        except aiomqtt.MqttError as e:
            logger.warning("MQTT error: %s — retrying in %ds", e, reconnect_delay)
        except Exception as e:
            logger.error("Unexpected MQTT error: %s", e)
        finally:
            state.mqtt_client    = None
            state.mqtt_connected = False
            await broadcast({"type": "mqtt_status", "connected": False})
            await asyncio.sleep(reconnect_delay)


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
        CLOUD_PATH.write_text(json.dumps(tuyadevices, indent=4, ensure_ascii=False), encoding="utf-8")

        await wizard_update("Done!", running=False, **load_init_data())

    except Exception as e:
        logger.error("Wizard error: %s", e)
        await wizard_update("Wizard failed.", running=False, error=str(e))


# ---------------------------------------------------------------------------
# Init data / WS command
# ---------------------------------------------------------------------------
def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception as e:
        logger.error("Error reading %s: %s", path, e)
        return default


def load_init_data() -> dict:
    raw = _load_json(CLOUD_PATH, [])
    items = raw if isinstance(raw, list) else list(raw.values())
    cloud_devices = {d["id"]: d for d in items if "id" in d}
    user_code     = (_load_json(CREDS_PATH, {}) or {}).get("user_code", "")
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
        asyncio.create_task(run_wizard(payload.get("user_code", "")))


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
async def fetch_config_from_mqtt() -> AppConfig:
    """Subscribe to the bridge config topic and return parsed AppConfig."""
    cfg = AppConfig()
    logger.info("Fetching config from %s", CONFIG_DISCOVERY_TOPIC)
    async with aiomqtt.Client(hostname=cfg.mqtt_broker, port=cfg.mqtt_port) as client:
        await client.subscribe(CONFIG_DISCOVERY_TOPIC)
        async with asyncio.timeout(5.0):
            async for message in client.messages:
                if str(message.topic) == CONFIG_DISCOVERY_TOPIC:
                    data = json.loads(message.payload.decode())
                    logger.info("Config received: %s", data)
                    cfg.update_from_dict(data)
                    return cfg
    raise RuntimeError(f"No config on {CONFIG_DISCOVERY_TOPIC} — is rustuya-bridge running?")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Prepare kwargs for the internal rustuya-bridge from environment variables
    bridge_kwargs = {}
    
    env_mapping = {
        "MQTT_BROKER": "mqtt_broker",
        "STATE_FILE": "state_file",
        "CONFIG": "config_path",
        "LOG_LEVEL": "log_level",
        "MQTT_ROOT_TOPIC": "mqtt_root_topic",
        "MQTT_COMMAND_TOPIC": "mqtt_command_topic",
        "MQTT_EVENT_TOPIC": "mqtt_event_topic",
        "MQTT_CLIENT_ID": "mqtt_client_id",
        "MQTT_MESSAGE_TOPIC": "mqtt_message_topic",
        "MQTT_PAYLOAD_TEMPLATE": "mqtt_payload_template",
        "MQTT_SCANNER_TOPIC": "mqtt_scanner_topic"
    }
    
    for env_key, kwarg_key in env_mapping.items():
        if val := os.getenv(env_key):
            bridge_kwargs[kwarg_key] = val
            
    # Set fallback defaults for essential paths if not provided
    bridge_kwargs.setdefault("mqtt_broker", "localhost")
    bridge_kwargs.setdefault("state_file", str(DATA_DIR / "rustuya.json"))
    bridge_kwargs.setdefault("config_path", str(DATA_DIR / "config.json"))
    bridge_kwargs.setdefault("log_level", "info")

    # Handle special types
    if "MQTT_RETAIN" in os.environ:
        bridge_kwargs["mqtt_retain"] = os.environ["MQTT_RETAIN"].lower() in ("true", "1", "yes", "t")
        
    if "SAVE_DEBOUNCE_SECS" in os.environ:
        try:
            bridge_kwargs["save_debounce_secs"] = int(os.environ["SAVE_DEBOUNCE_SECS"])
        except ValueError:
            pass

    bridge = PyBridgeServer(**bridge_kwargs)

    # Wrap in a coroutine to avoid TypeError since start_async returns a Future
    async def start_bridge():
        await bridge.start_async()

    bridge_task = asyncio.create_task(start_bridge())
    logger.info("Internal rustuya-bridge started.")

    # 2. Wait for config to be published via MQTT and start listener
    state.config = await fetch_config_from_mqtt()
    task = asyncio.create_task(mqtt_listener())
    
    yield
    
    # 3. Shutdown
    task.cancel()
    bridge_task.cancel()
    logger.info("Internal rustuya-bridge stopped.")


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
        await websocket.send_text(json.dumps({
            "type":           "init",
            "devices":        state.devices_map,
            "mqtt_connected": state.mqtt_connected,
            **load_init_data(),
        }))
        async for raw in websocket.iter_text():
            await handle_ws_command(json.loads(raw), websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS error: %s", e)
    finally:
        state.websocket_connections.discard(websocket)
