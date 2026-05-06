import asyncio
import json
import logging
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

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
BRIDGE_ACTIONS = {"add", "remove", "status", "query", "get"}
WS_DELETE_ALIAS = "delete"  # Frontend alias → 'remove'

# Keys to strip from bulk event/error payloads before storing in devices_map
_EVENT_JUNK_KEYS = frozenset({"errorCode", "errorMsg", "payloadStr", "id", "action"})

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
    mqtt_command_topic:    str       = "{root}/command"
    mqtt_event_topic:     str       = "{root}/event/{type}/{id}"
    mqtt_message_topic:   str       = "{root}/{level}/{id}"
    mqtt_scanner_topic:   str       = "{root}/scanner"
    mqtt_payload_template: str      = "{value}"

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
        ctx = {"id": "", "name": "", "cid": "", "level": "", "type": "", "dp": "", "action": "", "timestamp": int(time.time()), "value": kwargs.get("value") or kwargs.get("dps_str", ""), "dps": kwargs.get("dps_str", ""), **kwargs}
        res = template.replace("{root}", self.mqtt_root_topic)
        for k, v in ctx.items():
            if not v and f"{{{k}}}" in res:
                res = res.replace(f"/{{{k}}}", "")
            res = res.replace(f"{{{k}}}", str(v))
        return res

    def resolve_command_topic(self, action: str, device_id: str | None = None, dp: str | None = None) -> str:
        """Resolve command topic using standardized formatter with dummy support."""
        return self.format(self.mqtt_command_topic, action=action, id=device_id or "_", dp=dp or "_")

    def match_payload(self, payload: object) -> dict[str, Any]:
        """Match payload against template to extract id, dp, value, etc."""
        if not isinstance(payload, dict) or not isinstance(self.mqtt_payload_template, str):
            return {"value": payload}
        pattern = r'["\']?([\w-]+)["\']?\s*:\s*["\']?\{(\w+)\}["\']?'
        return {p: payload[k] for k, p in re.findall(pattern, self.mqtt_payload_template) if k in payload} or {"value": payload}

    def prepare_publish(self, action: str, payload: dict | None = None) -> tuple[str, str]:
        """Resolve topic and clean payload based on template."""
        payload = dict(payload or {})
        device_id = payload.get("id")
        dp = payload.get("dp")
        
        # 1. Resolve Topic (with dummy values)
        topic = self.resolve_command_topic(action, device_id=device_id, dp=dp)
        
        # 2. Deduplicate Payload
        # Remove fields that are already in the topic template
        template = self.mqtt_command_topic
        for key in ["action", "id", "dp"]:
            if f"{{{key}}}" in template:
                payload.pop(key, None)
        
        # 3. Finalize Payload String
        # Send 'null' if empty, otherwise JSON
        payload_str = "null" if not payload else json.dumps(payload)
        return topic, payload_str


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
    devs = payload.get("devices") or (payload.get("data", {}).get("devices") if isinstance(payload.get("data"), dict) else None)
    if isinstance(devs, list):
        return {d["id"]: d for d in devs if isinstance(d, dict) and "id" in d}
    return devs if isinstance(devs, dict) else None


def _match_template(template: str | None, root: str, topic: str) -> dict[str, Any] | None:
    if not template: return None
    tmpl_parts = template.replace("{root}", root).split("/")
    topic_parts = topic.split("/")
    if tmpl_parts[-1] == "#":
        tmpl_parts = tmpl_parts[:-1]
        topic_parts = topic_parts[:len(tmpl_parts)]
    if len(topic_parts) != len(tmpl_parts): return None
    captured = {}
    for t, p in zip(tmpl_parts, topic_parts):
        if t.startswith("{") and t.endswith("}"): captured[t[1:-1]] = p
        elif t != p: return None
    return captured


def classify_mqtt_topic(topic: str, payload: Any) -> tuple[str, dict[str, Any]]:
    cfg = state.config
    for template, base_type in [
        (cfg.mqtt_event_topic, "event"), (cfg.mqtt_message_topic, "message"),
        (cfg.mqtt_scanner_topic, "scanner"), (cfg.mqtt_command_topic, "command")
    ]:
        if (m := _match_template(template, cfg.mqtt_root_topic, topic)) is not None:
            m.update(cfg.match_payload(payload))
            sub = m.get("level") or m.get("type") or base_type
            match sub:
                case "response" | "error": return sub, m
                case _: return "event" if base_type == "event" else ("response" if base_type == "message" else base_type), m
    return "unknown", {}


def handle_mqtt_message(
    topic: str, payload: object, topic_type: str, captured_vars: dict[str, Any]
) -> tuple[bool, dict | None, bool]:
    """
    Update devices_map from an MQTT message.
    Returns: (devices_updated, snapshot | None, should_request_status)
    """
    did = captured_vars.get("id")
    action = payload.get("action") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and did and not payload.get("id"):
        payload["id"] = did
    did = did or (payload.get("id") if isinstance(payload, dict) else None)
    if not did: return False, None, False

    match topic_type, payload:
        case "response", dict(payload):
            if devs := extract_devices(payload):
                state.devices_map = devs
                return True, dict(state.devices_map), False
            status = str(payload.get("status", "")).lower()
            is_success = status in ("success", "ok") or payload.get("status") is True
            match action, is_success:
                case "remove", True:
                    state.devices_map.pop(did, None)
                    return True, dict(state.devices_map), True
                case "add", True:
                    return False, None, True
                case _: return False, None, False
                
        case ("error" | "event") as t, _ if not action:
            filtered = {"status": "online"} if t == "event" else {}
            if "dp" in captured_vars:
                filtered[str(captured_vars["dp"])] = captured_vars.get("value") or payload
            elif isinstance(payload, dict):
                filtered = {k: v for k, v in payload.items() if k not in _EVENT_JUNK_KEYS}
                if t == "error" and "errorCode" in payload:
                    filtered["status"] = "online" if payload["errorCode"] == 0 else str(payload["errorCode"])
            else:
                return False, None, False

            if did in state.devices_map:
                state.devices_map[did].update(filtered)
            else:
                state.devices_map[did] = {**filtered, "id": did}
            return True, dict(state.devices_map), False

        case _: return False, None, False


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

                    topic_type, captured_vars          = classify_mqtt_topic(topic, payload)
                    if topic_type in ("command", "unknown"):
                        continue  # command: our own echo; unknown: unrecognised topic

                    devices_updated, snapshot, refresh = handle_mqtt_message(topic, payload, topic_type, captured_vars)

                    if refresh:
                        target_id = refresh if isinstance(refresh, str) else None
                        refresh_action = "get" if target_id else "status"
                        cmd_topic, cmd_payload = cfg.prepare_publish(refresh_action, {"id": target_id} if target_id else None)
                        logger.info("Auto-requesting '%s' for device: %s", refresh_action, target_id or "all")
                        await client.publish(cmd_topic, cmd_payload)

                    # Determine live value for UI display
                    live_dp = captured_vars.get("dp")
                    live_val = captured_vars.get("value")
                    if live_dp and (live_val is None or live_val == ""):
                        live_val = payload

                    await broadcast({
                        "type":            "mqtt",
                        "topic_type":      topic_type,
                        "topic":           topic,
                        "payload":         payload,
                        "id":              captured_vars.get("id"),
                        "dp":              live_dp,
                        "value":           live_val,
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
    action, payload = cmd.get("action", ""), cmd.get("payload", {})
    action = "remove" if action == WS_DELETE_ALIAS else action
    match action:
        case _ if action in BRIDGE_ACTIONS:
            if state.mqtt_client:
                await state.mqtt_client.publish(*state.config.prepare_publish(action, payload))
            else:
                await send_to(websocket, {"type": "bridge_response", "level": "error", "message": "MQTT broker not connected."})
        case "wizard_start":
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
            discovery_topic = cfg.format("{root}/bridge/config")
            logger.info("Fetching config from %s (via %s:%d)...", discovery_topic, cfg.mqtt_broker, cfg.mqtt_port)
            async with aiomqtt.Client(
                hostname=cfg.mqtt_broker, 
                port=cfg.mqtt_port,
                username=cfg.mqtt_user,
                password=cfg.mqtt_password
            ) as client:
                await client.subscribe(discovery_topic)
                async with asyncio.timeout(5.0):
                    async for message in client.messages:
                        if str(message.topic) == discovery_topic:
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
            sleep_task = asyncio.ensure_future(asyncio.sleep(reconnect_delay))
            tasks_to_wait = [sleep_task]
            if stop:
                stop_task = asyncio.ensure_future(stop.wait())
                tasks_to_wait.append(stop_task)
            try:
                await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks_to_wait:
                    t.cancel()
            if stop and stop.is_set():
                raise asyncio.CancelledError("Shutdown requested")

            reconnect_delay = min(reconnect_delay * 2, MAX_RETRY_DELAY_SECS)


def _build_bridge_kwargs() -> dict:
    config_path = Path(os.environ.get("CONFIG", DATA_DIR / "config.json"))
    kwargs = {
        "mqtt_broker": "localhost", "state_file": str(DATA_DIR / "rustuya.json"),
        "config_path": str(config_path), "log_level": "info", "no_signals": True,
    }
    if config_path.exists():
        try:
            if isinstance(file_config := json.loads(config_path.read_text(encoding="utf-8")), dict):
                kwargs.update(file_config)
                logger.info("Loaded bridge settings from %s", config_path)
        except Exception as e: logger.warning("Failed to read config %s: %s", config_path, e)

    kwargs.update({k: os.environ[e] for e, k in ENV_TO_KWARG.items() if e in os.environ})
    if "MQTT_RETAIN" in os.environ: kwargs["mqtt_retain"] = os.environ["MQTT_RETAIN"].lower() in ("true", "1", "yes", "t")
    if "SAVE_DEBOUNCE_SECS" in os.environ:
        try: kwargs["save_debounce_secs"] = int(os.environ["SAVE_DEBOUNCE_SECS"])
        except ValueError: pass
    return kwargs

async def _shutdown_cleanup(task, bridge_task, bridge):
    logger.info("Shutting down manager (cleanup phase 1/3)...")
    if task:
        task.cancel()
        try: await task
        except Exception: pass
    logger.info("Shutting down manager (cleanup phase 2/3)...")
    bridge_task.cancel()
    try: await asyncio.wait_for(bridge_task, timeout=2.0)
    except Exception: pass
    logger.info("Shutting down manager (cleanup phase 3/3: closing bridge)...")
    try:
        await bridge.close()
        logger.info("Internal rustuya-bridge stopped.")
    except Exception as e: logger.error("Error during bridge close: %s", e)

@asynccontextmanager
async def lifespan(app: FastAPI):
    bridge_kwargs = _build_bridge_kwargs()
    bridge = PyBridgeServer(**bridge_kwargs)
    bridge_task = asyncio.create_task(bridge.start_async())
    logger.info("Internal rustuya-bridge started with: %s", {k: v for k, v in bridge_kwargs.items() if "password" not in k})

    init_cfg = AppConfig()
    init_cfg.update_from_dict(bridge_kwargs)

    _stop, loop = asyncio.Event(), asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _stop.set)
        loop.add_signal_handler(signal.SIGTERM, _stop.set)
    except (NotImplementedError, OSError): pass

    task = None
    try:
        try:
            state.config, config_data = await fetch_config_from_mqtt(init_cfg, stop=_stop)
        finally:
            try:
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            except (NotImplementedError, OSError): pass

        config_path = Path(bridge_kwargs["config_path"])
        if not config_path.exists():
            logger.info("Saving received config to %s", config_path)
            await asyncio.to_thread(config_path.write_text, json.dumps(config_data, indent=4, ensure_ascii=False), encoding="utf-8")

        task = asyncio.create_task(mqtt_listener())
        yield
    finally:
        cleanup_task = asyncio.create_task(_shutdown_cleanup(task, bridge_task, bridge))
        try: await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            logger.warning("Shutdown signal received during cleanup, waiting...")
            try: await asyncio.wait_for(cleanup_task, timeout=10.0)
            except Exception as e: logger.error("Cleanup failed: %s", e)
        except Exception as e: logger.error("Unexpected error during cleanup: %s", e)


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

