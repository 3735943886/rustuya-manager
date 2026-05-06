import asyncio
import json
import logging
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiomqtt
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ConfigDict
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

BRIDGE_ACTIONS = {"add", "remove", "status", "query", "get"}
WS_DELETE_ALIAS = "delete"

_EVENT_JUNK_KEYS = frozenset({"errorCode", "errorMsg", "payloadStr", "id", "action"})
INITIAL_RETRY_DELAY = 10
MAX_RETRY_DELAY = 1280

ENV_MAP = {
    "MQTT_BROKER": "mqtt_broker", "STATE_FILE": "state_file", "CONFIG": "config_path",
    "LOG_LEVEL": "log_level", "MQTT_USER": "mqtt_user", "MQTT_PASSWORD": "mqtt_password",
    "MQTT_ROOT_TOPIC": "mqtt_root_topic", "MQTT_COMMAND_TOPIC": "mqtt_command_topic",
    "MQTT_EVENT_TOPIC": "mqtt_event_topic", "MQTT_CLIENT_ID": "mqtt_client_id",
    "MQTT_MESSAGE_TOPIC": "mqtt_message_topic", "MQTT_PAYLOAD_TEMPLATE": "mqtt_payload_template",
    "MQTT_SCANNER_TOPIC": "mqtt_scanner_topic"
}


# ---------------------------------------------------------------------------
# Configuration & State Models (Pydantic)
# ---------------------------------------------------------------------------
class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    mqtt_root_topic: str = "rustuya"
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    mqtt_command_topic: str = "{root}/command"
    mqtt_event_topic: str = "{root}/event/{type}/{id}"
    mqtt_message_topic: str = "{root}/{level}/{id}"
    mqtt_scanner_topic: str = "{root}/scanner"
    mqtt_payload_template: str = "{value}"

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        if broker := data.get("mqtt_broker"):
            broker = broker.split("://")[-1]
            host, *rest = broker.split(":")
            data["mqtt_broker"] = host
            if rest: data["mqtt_port"] = int(rest[0])
        return cls(**data)

    def format_topic(self, template: str, **kwargs) -> str:
        res = template.replace("{root}", self.mqtt_root_topic)
        ctx = {"id": "", "name": "", "cid": "", "level": "", "type": "", "dp": "", "action": "", "timestamp": int(time.time()), "value": kwargs.get("value", kwargs.get("dps_str", "")), "dps": kwargs.get("dps_str", ""), **kwargs}
        for k, v in ctx.items():
            key = f"{{{k}}}"
            if key in res:
                val = str(v)
                res = res.replace(f"/{key}", "") if not val else res.replace(key, val)
        return res

    def extract_payload_vars(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not self.mqtt_payload_template or self.mqtt_payload_template == "{value}":
            return {"value": payload}
        try:
            tpl_str = json.dumps(self.mqtt_payload_template) if isinstance(self.mqtt_payload_template, dict) else self.mqtt_payload_template
            matches = re.findall(r'["\']?([\w-]+)["\']?\s*:\s*["\']?\{([\w]+)\}["\']?', tpl_str)
            return {placeholder: payload[key] for key, placeholder in matches if key in payload} or {"value": payload}
        except Exception:
            return {"value": payload}

    def prepare_publish(self, action: str, payload: dict | None = None) -> tuple[str, str]:
        p = dict(payload or {})
        topic = self.format_topic(self.mqtt_command_topic, action=action, id=p.get("id", "_"), dp=p.get("dp", "_"))
        for k in ["action", "id", "dp"]:
            if f"{{{k}}}" in self.mqtt_command_topic: p.pop(k, None)
        return topic, json.dumps(p) if p else "null"


class AppState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    config: AppConfig = Field(default_factory=AppConfig)
    devices_map: dict = Field(default_factory=dict)
    websocket_connections: set[WebSocket] = Field(default_factory=set)
    background_tasks: set[asyncio.Task] = Field(default_factory=set)
    mqtt_client: aiomqtt.Client | None = None
    mqtt_connected: bool = False


state = AppState()


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------
async def broadcast(message: dict) -> None:
    if not state.websocket_connections: return
    payload = json.dumps(message)
    for ws in list(state.websocket_connections):
        try: await ws.send_text(payload)
        except Exception: state.websocket_connections.discard(ws)

async def send_to(ws: WebSocket, message: dict) -> None:
    try: await ws.send_text(json.dumps(message))
    except Exception: state.websocket_connections.discard(ws)


# ---------------------------------------------------------------------------
# MQTT message helpers
# ---------------------------------------------------------------------------
def extract_devices(payload: dict) -> dict | None:
    devs = payload.get("devices") or (payload.get("data") or {}).get("devices")
    if devs is None: return None
    return {d["id"]: d for d in devs if isinstance(d, dict) and "id" in d} if isinstance(devs, list) else (devs if isinstance(devs, dict) else {})

def match_template(template: str, root: str, topic: str) -> dict[str, str] | None:
    tmpl_parts, topic_parts = template.replace("{root}", root).split("/"), topic.split("/")
    is_wildcard = tmpl_parts[-1] == "#"
    if is_wildcard:
        tmpl_parts = tmpl_parts[:-1]
        topic_parts = topic_parts[:len(tmpl_parts)]
    
    if len(topic_parts) != len(tmpl_parts): return None
    
    captured = {}
    for tmpl_seg, topic_seg in zip(tmpl_parts, topic_parts):
        if tmpl_seg.startswith("{") and tmpl_seg.endswith("}"): captured[tmpl_seg[1:-1]] = topic_seg
        elif tmpl_seg != topic_seg: return None
    return captured

def classify_mqtt_topic(topic: str, payload: Any) -> tuple[str, dict[str, Any]]:
    rules = [
        (state.config.mqtt_event_topic, "event"), (state.config.mqtt_message_topic, "message"),
        (state.config.mqtt_scanner_topic, "scanner"), (state.config.mqtt_command_topic, "command")
    ]
    for template, base_type in rules:
        if (m := match_template(template, state.config.mqtt_root_topic, topic)) is not None:
            m.update(state.config.extract_payload_vars(payload))
            sub = m.get("level") or m.get("type") or base_type
            if sub in ("response", "error"): return sub, m
            return "event" if base_type == "event" else ("response" if base_type == "message" else base_type), m
    return "unknown", {}

def handle_mqtt_message(topic_type: str, captured: dict, payload: Any) -> tuple[bool, dict | None, str | None]:
    did = captured.get("id")
    action = payload.get("action") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and not payload.get("id") and did: payload["id"] = did
    did = did or (payload.get("id") if isinstance(payload, dict) else None)
    
    if not did: return False, None, None

    match topic_type:
        case "response" if isinstance(payload, dict):
            if (devs := extract_devices(payload)) is not None:
                state.devices_map = devs
                return True, dict(state.devices_map), None
            status = str(payload.get("status", "")).lower()
            success = status in ("success", "ok") or payload.get("status") is True
            if action == "remove" and success:
                state.devices_map.pop(did, None)
                return True, dict(state.devices_map), "status"
            if action == "add" and success:
                return False, None, "get"
        case "error" | "event" if not action:
            filtered = {"status": "online"} if topic_type == "event" else {}
            if "dp" in captured:
                filtered[str(captured["dp"])] = captured.get("value") if captured.get("value") not in (None, "") else payload
            elif isinstance(payload, dict):
                filtered = {k: v for k, v in payload.items() if k not in _EVENT_JUNK_KEYS}
                if topic_type == "error" and "errorCode" in payload:
                    filtered["status"] = "online" if payload["errorCode"] == 0 else str(payload["errorCode"])
            else:
                return False, None, None
            
            if did in state.devices_map: state.devices_map[did].update(filtered)
            else: state.devices_map[did] = {**filtered, "id": did}
            return True, dict(state.devices_map), None

    return False, None, None


# ---------------------------------------------------------------------------
# MQTT listener
# ---------------------------------------------------------------------------
async def mqtt_listener() -> None:
    delay = INITIAL_RETRY_DELAY
    while True:
        try:
            async with aiomqtt.Client(state.config.mqtt_broker, port=state.config.mqtt_port, username=state.config.mqtt_user, password=state.config.mqtt_password) as client:
                state.mqtt_client, state.mqtt_connected, delay = client, True, INITIAL_RETRY_DELAY
                await broadcast({"type": "mqtt_status", "connected": True})
                await client.subscribe(f"{state.config.mqtt_root_topic}/#")

                async for msg in client.messages:
                    p = msg.payload.decode()
                    try: payload = json.loads(p) if p.startswith(("{", "[")) else p
                    except Exception: payload = p

                    ttype, captured = classify_mqtt_topic(str(msg.topic), payload)
                    logger.debug("MQTT: %s | Type: %s | Vars: %s", msg.topic, ttype, captured)
                    if ttype in ("command", "unknown"): continue
                    
                    updated, snapshot, refresh = handle_mqtt_message(ttype, captured, payload)
                    if updated: logger.info("Devices updated via %s (count: %d)", ttype, len(snapshot))
                    if refresh:
                        target = captured.get("id") if refresh == "get" else None
                        cmd_topic, cmd_payload = state.config.prepare_publish(refresh, {"id": target} if target else None)
                        await client.publish(cmd_topic, cmd_payload)

                    live_dp, live_val = captured.get("dp"), captured.get("value")
                    if live_dp and live_val in (None, ""): live_val = payload

                    await broadcast({
                        "type": "mqtt", "topic_type": ttype, "topic": str(msg.topic), "payload": payload,
                        "id": captured.get("id"), "dp": live_dp, "value": live_val,
                        "devices_updated": updated, "devices": snapshot
                    })
        except asyncio.CancelledError: raise
        except Exception as e:
            logger.error("MQTT Error: %s — retrying in %ds", e, delay)
            state.mqtt_client, state.mqtt_connected = None, False
            await broadcast({"type": "mqtt_status", "connected": False})
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)


# ---------------------------------------------------------------------------
# Wizard & Data Loading
# ---------------------------------------------------------------------------
async def run_wizard(user_code: str) -> None:
    from tuyawizard.wizard import TuyaWizard, postprocess_devices
    loop = asyncio.get_running_loop()
    
    async def wizard_update(step: str, running: bool = True, **extra):
        await broadcast({"type": "wizard", "status": {"running": running, "step": step, **extra}, **extra})

    def qr_callback(url: str | None):
        asyncio.run_coroutine_threadsafe(wizard_update("Waiting for app scan..." if url else "Fetching devices...", url=url), loop)

    try:
        await wizard_update("Starting API Login...")
        tuya = TuyaWizard(info_file=str(CREDS_PATH))
        await asyncio.to_thread(tuya.login_auto, user_code=user_code, qr_callback=qr_callback)
        
        await wizard_update("Fetching devices...")
        tuyadevices = await asyncio.to_thread(tuya.fetch_devices)
        
        await wizard_update("Matching sub-devices...")
        await asyncio.to_thread(postprocess_devices, tuyadevices, "all")
        
        await wizard_update("Saving devices...")
        await asyncio.to_thread(CLOUD_PATH.write_text, json.dumps(tuyadevices, indent=4, ensure_ascii=False), encoding="utf-8")
        
        await wizard_update("Done!", running=False, **await load_init_data())
    except Exception as e:
        logger.error("Wizard error: %s", e)
        await wizard_update("Wizard failed.", running=False, error=str(e))

async def _load_json(path: Path, default: Any = None) -> Any:
    try: return json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    except Exception: return default

async def load_init_data() -> dict:
    raw = await _load_json(CLOUD_PATH, [])
    cloud_devices = {d["id"]: d for d in (raw if isinstance(raw, list) else list(raw.values())) if isinstance(d, dict) and "id" in d}
    creds = await _load_json(CREDS_PATH, {})
    return {"cloud_devices": cloud_devices, "user_code": creds.get("user_code", "") if isinstance(creds, dict) else ""}

async def handle_ws_command(cmd: dict, ws: WebSocket) -> None:
    action, payload = cmd.get("action", ""), cmd.get("payload", {})
    action = "remove" if action == WS_DELETE_ALIAS else action
    
    if action in BRIDGE_ACTIONS:
        if state.mqtt_client:
            await state.mqtt_client.publish(*state.config.prepare_publish(action, payload))
        else:
            await send_to(ws, {"type": "bridge_response", "level": "error", "message": "MQTT not connected."})
    elif action == "wizard_start":
        task = asyncio.create_task(run_wizard(payload.get("user_code", "")))
        state.background_tasks.add(task)
        task.add_done_callback(state.background_tasks.discard)


# ---------------------------------------------------------------------------
# FastAPI & Lifespan
# ---------------------------------------------------------------------------
async def fetch_mqtt_config(cfg: AppConfig, stop: asyncio.Event) -> tuple[AppConfig, dict]:
    delay = INITIAL_RETRY_DELAY
    while True:
        if stop.is_set(): raise asyncio.CancelledError()
        try:
            topic = cfg.format_topic("{root}/bridge/config")
            async with aiomqtt.Client(cfg.mqtt_broker, port=cfg.mqtt_port, username=cfg.mqtt_user, password=cfg.mqtt_password) as client:
                await client.subscribe(topic)
                async with asyncio.timeout(5.0):
                    async for msg in client.messages:
                        if str(msg.topic) == topic:
                            data = json.loads(msg.payload.decode())
                            return AppConfig.from_dict(data), data
        except Exception:
            try: await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError: delay = min(delay * 2, MAX_RETRY_DELAY)
            if stop.is_set(): raise asyncio.CancelledError()

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg_path = Path(os.environ.get("CONFIG", DATA_DIR / "config.json"))
    bridge_kwargs = {"mqtt_broker": "localhost", "state_file": str(DATA_DIR / "rustuya.json"), "config_path": str(cfg_path), "log_level": "info", "no_signals": True}
    
    if cfg_path.exists():
        if isinstance(f_cfg := await _load_json(cfg_path), dict): bridge_kwargs.update(f_cfg)
    
    bridge_kwargs.update({k: os.environ[e] for e, k in ENV_MAP.items() if e in os.environ})
    if "MQTT_RETAIN" in os.environ: bridge_kwargs["mqtt_retain"] = os.environ["MQTT_RETAIN"].lower() in ("true", "1", "yes", "t")
    if "SAVE_DEBOUNCE_SECS" in os.environ:
        try: bridge_kwargs["save_debounce_secs"] = int(os.environ["SAVE_DEBOUNCE_SECS"])
        except ValueError: pass

    bridge = PyBridgeServer(**bridge_kwargs)

    async def start_bridge():
        await bridge.start_async()

    bridge_task = asyncio.create_task(start_bridge())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop_event.set)
        except Exception: pass

    listener_task = None
    try:
        state.config, config_data = await fetch_mqtt_config(AppConfig.from_dict(bridge_kwargs), stop_event)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try: loop.remove_signal_handler(sig)
            except Exception: pass

        if not cfg_path.exists():
            await asyncio.to_thread(cfg_path.write_text, json.dumps(config_data, indent=4), encoding="utf-8")
        
        listener_task = asyncio.create_task(mqtt_listener())
        yield
    finally:
        async def cleanup():
            if listener_task: listener_task.cancel()
            bridge_task.cancel()
            try: await bridge.close()
            except Exception: pass
        
        try: await asyncio.wait_for(asyncio.shield(cleanup()), timeout=10.0)
        except Exception: pass


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

@app.get("/")
async def get_index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.websocket_connections.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "init", "devices": state.devices_map, "mqtt_connected": state.mqtt_connected, **await load_init_data()}))
        async for raw in ws.iter_text(): await handle_ws_command(json.loads(raw), ws)
    except WebSocketDisconnect: pass
    finally: state.websocket_connections.discard(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8373)))
