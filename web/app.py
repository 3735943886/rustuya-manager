import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
import io
import base64

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiomqtt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rustuya-web")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent
CONFIG_PATH = DATA_DIR / "config.json"
CLOUD_PATH  = DATA_DIR / "tuyadevices.json"
CREDS_PATH  = DATA_DIR / "tuyacreds.json"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    root_topic: str = "rustuya"
    mqtt_command_topic: str = "{root}/command"
    mqtt_event_topic: str = "{root}/event/{type}"
    mqtt_message_topic: str | None = None
    mqtt_scanner_topic: str | None = None

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        if not CONFIG_PATH.exists():
            return cfg
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
            
            broker_full = data.get("mqtt_broker", "localhost:1883")
            if "://" in broker_full:
                broker_full = broker_full.split("://")[-1]
            parts = broker_full.split(":")
            cfg.mqtt_broker = parts[0]
            if len(parts) > 1:
                cfg.mqtt_port = int(parts[1])
                
            cfg.root_topic          = data.get("mqtt_root_topic", "rustuya")
            cfg.mqtt_command_topic  = data.get("mqtt_command_topic", "{root}/command")
            cfg.mqtt_event_topic    = data.get("mqtt_event_topic",   "{root}/event/{type}")
            cfg.mqtt_message_topic  = data.get("mqtt_message_topic")
            cfg.mqtt_scanner_topic  = data.get("mqtt_scanner_topic")
        except Exception as e:
            logger.error("Error loading config: %s", e)
        return cfg


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
@dataclass
class AppState:
    config: AppConfig = field(default_factory=AppConfig)
    devices_map: dict = field(default_factory=dict)
    websocket_connections: set = field(default_factory=set)
    mqtt_client: "aiomqtt.Client | None" = None
    mqtt_connected: bool = False


state = AppState()


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------
async def broadcast(message: dict) -> None:
    if not state.websocket_connections:
        return
    msg_str = json.dumps(message)
    results = await asyncio.gather(
        *(ws.send_text(msg_str) for ws in state.websocket_connections),
        return_exceptions=True,
    )
    dead = {
        ws
        for ws, result in zip(list(state.websocket_connections), results)
        if isinstance(result, Exception)
    }
    state.websocket_connections -= dead


async def send_to(websocket: WebSocket, message: dict) -> None:
    try:
        await websocket.send_text(json.dumps(message))
    except Exception:
        state.websocket_connections.discard(websocket)


# ---------------------------------------------------------------------------
# MQTT message processing helpers
# ---------------------------------------------------------------------------
def extract_devices(payload: dict) -> dict | None:
    """Extract device list/dict from various payload shapes."""
    # Check explicitly for existence of "devices" key to handle empty results
    has_devices = "devices" in payload
    devs = payload.get("devices")

    if not has_devices and "data" in payload and isinstance(payload["data"], dict):
        if "devices" in payload["data"]:
            has_devices = True
            devs = payload["data"]["devices"]

    if not has_devices:
        return None

    if isinstance(devs, list):
        return {d["id"]: d for d in devs if "id" in d}
    if isinstance(devs, dict):
        return devs
    return {}


def classify_mqtt_topic(topic: str) -> str:
    """
    Classify an incoming MQTT topic based on patterns in config.json.
    Returns: 'response' | 'error' | 'scanner' | 'event'
    """
    cfg = state.config
    root = cfg.root_topic
    
    # 1. Custom message topic check (response/error)
    if cfg.mqtt_message_topic:
        # Template might be e.g. "{root}/{level}/{id}"
        # We check if it matches the prefix or general structure
        msg_base = cfg.mqtt_message_topic.replace("{root}", root).split("{")[0].rstrip('/')
        if msg_base and topic.startswith(msg_base):
            if "/error" in topic: return "error"
            return "response"

    # 2. Custom scanner topic check
    if cfg.mqtt_scanner_topic:
        scanner_base = cfg.mqtt_scanner_topic.replace("{root}", root)
        if topic == scanner_base or topic.startswith(f"{scanner_base}/"):
            return "scanner"

    # Normalize by adding leading slash for easier matching
    t = f"/{topic}"
    r = f"/{root}"

    # 3. Response check (e.g., rustuya/response or rustuya/response/id)
    if f"{r}/response" in t:
        return "response"
        
    # 4. Error check (e.g., rustuya/error or rustuya/error/id)
    if f"{r}/error" in t:
        return "error"

    # 5. Scanner check
    if "/scanner" in t or topic == "scanner":
        return "scanner"

    # 6. Event topic check (Live messages)
    event_base = cfg.mqtt_event_topic.replace("{root}", root).replace("{type}", "")
    if topic.startswith(event_base) or f"{r}/event" in t:
        return "event"

    # 7. Default fallback
    return "event"


def handle_mqtt_message(topic: str, payload, topic_type: str) -> tuple[bool, dict | None, bool]:
    """
    Process a decoded MQTT payload and update devices_map in place.
    Returns: (devices_updated, updated_devices_snapshot | None, should_refresh_status)
    """
    should_refresh = False
    if not isinstance(payload, dict):
        return False, None, False

    action = payload.get("action")
    did    = payload.get("id")

    # 1. Handle "response" topic
    if topic_type == "response":
        # Full device list update
        devs = extract_devices(payload)
        if devs is not None:
            state.devices_map = devs
            return True, dict(state.devices_map)
        
        # Specific action confirmations
        status = str(payload.get("status", "")).lower()
        is_success = status in ("success", "ok") or payload.get("status") is True
        
        if action == "remove" and is_success and did:
            if did in state.devices_map:
                logger.info("Removing device %s from local state", did)
                del state.devices_map[did]
                return True, dict(state.devices_map), True
        
        if action == "add" and is_success:
            logger.info("Device add successful, triggering status refresh")
            return False, None, True
            
        return False, None, False

    # 2. Handle "error" or "event" topics
    if did and topic_type in ("error", "event"):
        # If the payload contains an "action", it's likely a bridge response/error 
        # about a specific command (like "add", "remove"), NOT a device state update.
        if "action" in payload:
            return False, None, False

        # Ignore junk keys to keep devices_map clean
        ignore = {"errorCode", "errorMsg", "payloadStr"}
        filtered = {k: v for k, v in payload.items() if k not in ignore}

        # Map errorCode to a standard status for the UI
        if topic_type == "error" and "errorCode" in payload:
            ecode = payload["errorCode"]
            filtered["status"] = "online" if ecode == 0 else str(ecode)

        if did in state.devices_map:
            # Update existing device (status from errors or properties from events)
            state.devices_map[did].update(filtered)
        else:
            # Auto-discover or sync device from bridge reporting
            logger.info("Discovered device from %s: %s (%s)", topic_type, filtered.get("name", "Unknown"), did)
            state.devices_map[did] = filtered
            
        return True, dict(state.devices_map), False


    return False, None, False


# ---------------------------------------------------------------------------
# MQTT listener
# ---------------------------------------------------------------------------
async def mqtt_listener() -> None:
    cfg = state.config
    reconnect_delay = 5

    while True:
        try:
            async with aiomqtt.Client(hostname=cfg.mqtt_broker, port=cfg.mqtt_port) as client:
                state.mqtt_client    = client
                state.mqtt_connected = True
                logger.info("Connected to MQTT broker at %s:%d", cfg.mqtt_broker, cfg.mqtt_port)
                await broadcast({"type": "mqtt_status", "connected": True})

                status_topic = (
                    cfg.mqtt_command_topic
                    .replace("{action}", "status")
                    .replace("{root}", cfg.root_topic)
                )
                await client.publish(status_topic, json.dumps({"action": "status"}))
                await client.subscribe(f"{cfg.root_topic}/#")

                async for message in client.messages:
                    topic   = str(message.topic)
                    try:
                        payload = json.loads(message.payload.decode())
                    except Exception:
                        payload = message.payload.decode()

                    topic_type = classify_mqtt_topic(topic)
                    devices_updated, updated_devices, should_refresh = handle_mqtt_message(topic, payload, topic_type)

                    if should_refresh:
                        status_topic = (
                            cfg.mqtt_command_topic
                            .replace("{action}", "status")
                            .replace("{root}", cfg.root_topic)
                        )
                        await client.publish(status_topic, json.dumps({"action": "status"}))

                    await broadcast({
                        "type":            "mqtt",
                        "topic_type":      topic_type,
                        "topic":           topic,
                        "payload":         payload,
                        "devices_updated": devices_updated,
                        "devices":         updated_devices,
                    })

        except aiomqtt.MqttError as e:
            logger.warning("MQTT connection failed: %s. Retrying in %ds...", e, reconnect_delay)
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

    async def update_wizard(step: str, *, url: str | None = None, qr_image: str | None = None, 
                             running: bool = True, error: str | None = None, **extra):
        status = {"running": running, "step": step, "url": url, "qr_image": qr_image}
        if error is not None:
            status["error"] = error
        await broadcast({"type": "wizard", "status": status, **extra})

    loop = asyncio.get_running_loop()

    def qr_callback(url: str | None):
        step = "Waiting for app scan..." if url else "Fetching devices..."
        asyncio.run_coroutine_threadsafe(update_wizard(step, url=url), loop)

    try:
        await update_wizard("Starting API Login...")
        tuya = TuyaWizard(info_file=str(CREDS_PATH))
        await asyncio.to_thread(tuya.login_auto, user_code=user_code, qr_callback=qr_callback)

        await update_wizard("Fetching devices...")
        tuyadevices = await asyncio.to_thread(tuya.fetch_devices)

        await update_wizard("Applying post-process (matching subdevices and scanning IPs)...")
        await asyncio.to_thread(postprocess_devices, tuyadevices, "all")

        await update_wizard("Saving devices...")
        with CLOUD_PATH.open("w", encoding="utf-8") as f:
            json.dump(tuyadevices, f, indent=4, ensure_ascii=False)

        # Broadcast completion WITH refreshed cloud_devices
        await update_wizard("Wizard Complete! Refreshing devices...", running=False,
                             **load_init_data())

    except Exception as e:
        logger.error("Wizard error: %s", e)
        await update_wizard("Wizard failed.", running=False, error=str(e))


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------
def load_init_data() -> dict:
    """Load cloud devices and user_code for the WebSocket init payload."""
    cloud_devices = {}
    user_code     = ""

    if CLOUD_PATH.exists():
        try:
            with CLOUD_PATH.open() as f:
                data = json.load(f)
            dl = data if isinstance(data, list) else list(data.values())
            cloud_devices = {d["id"]: d for d in dl if "id" in d}
        except Exception as e:
            logger.error("Error loading cloud file: %s", e)

    if CREDS_PATH.exists():
        try:
            with CREDS_PATH.open() as f:
                user_code = json.load(f).get("user_code", "")
        except Exception as e:
            logger.error("Error loading creds file: %s", e)

    return {"cloud_devices": cloud_devices, "user_code": user_code}


async def handle_ws_command(cmd: dict, websocket: WebSocket) -> None:
    """Dispatch a WebSocket command to the correct handler."""
    action  = cmd.get("action", "")
    payload = cmd.get("payload", {})

    bridge_actions = {"add", "remove", "status", "query", "get", "delete"}
    if action in bridge_actions:
        if action == "delete":
            action = "remove"
        payload["action"] = action
        if state.mqtt_client:
            pub_topic = (
                state.config.mqtt_command_topic
                .replace("{action}", action)
                .replace("{root}", state.config.root_topic)
            )
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
@asynccontextmanager
async def lifespan(app: FastAPI):
    state.config = AppConfig.load()
    task = asyncio.create_task(mqtt_listener())
    yield
    task.cancel()


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
        init_data = load_init_data()
        await websocket.send_text(json.dumps({
            "type":          "init",
            "devices":       state.devices_map,
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
