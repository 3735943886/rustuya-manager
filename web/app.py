import asyncio
import json
import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import aiomqtt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rustuya-web")

# Globals for state
devices_map = {}
websocket_connections = set()
mqtt_client = None

# Config defaults
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent
CONFIG_PATH = DATA_DIR / "config.json"
CLOUD_PATH = DATA_DIR / "tuyadevices.json"
CREDS_PATH = DATA_DIR / "tuyacreds.json"

# We will try to load config
mqtt_broker = "127.0.0.1"
mqtt_port = 1883
root_topic = "rustuya"
mqtt_command_topic = f"{root_topic}/command"
mqtt_event_topic = f"{root_topic}/event"

def load_config():
    global mqtt_broker, mqtt_port, root_topic, mqtt_command_topic, mqtt_event_topic
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                config = json.load(f)
                broker_full = config.get("mqtt_broker", mqtt_broker)
                if '://' in broker_full:
                    broker_full = broker_full.split('://')[-1]
                parts = broker_full.split(':')
                mqtt_broker = parts[0]
                if len(parts) > 1:
                    mqtt_port = int(parts[1])
                root_topic = config.get("mqtt_root_topic", root_topic)
                mqtt_command_topic = config.get("mqtt_command_topic", f"{root_topic}/command")
                mqtt_event_topic = config.get("mqtt_event_topic", f"{root_topic}/event/{{type}}")
        except Exception as e:
            logger.error(f"Error loading config: {e}")

async def broadcast(message: dict):
    msg_str = json.dumps(message)
    disconnected = set()
    for ws in websocket_connections:
        try:
            await ws.send_text(msg_str)
        except:
            disconnected.add(ws)
    for ws in disconnected:
        websocket_connections.remove(ws)

async def mqtt_listener():
    global mqtt_client
    reconnect_delay = 5
    while True:
        try:
            async with aiomqtt.Client(hostname=mqtt_broker, port=mqtt_port) as client:
                mqtt_client = client
                logger.info(f"Connected to MQTT broker at {mqtt_broker}:{mqtt_port}")
                
                # Request full status on connect
                await client.publish(mqtt_command_topic.replace("{action}", "status").replace("{root}", root_topic), json.dumps({"action": "status"}))
                
                # Subscribe to root and events
                await client.subscribe(f"{root_topic}/#")
                
                async for message in client.messages:
                    topic = str(message.topic)
                    try:
                        payload = json.loads(message.payload.decode())
                    except:
                        payload = message.payload.decode()
                        
                    # Handle device snapshot/updates
                    devices_updated = False
                    if "devices" in payload or ("data" in payload and "devices" in payload["data"]):
                        devs = payload.get("devices") or payload.get("data", {}).get("devices")
                        if devs:
                            if isinstance(devs, list):
                                for dev in devs:
                                    did = dev.get("id")
                                    if did:
                                        devices_map[did] = dev
                            elif isinstance(devs, dict):
                                for did, d in devs.items():
                                    devices_map[did] = d
                            devices_updated = True
                    elif "id" in payload:
                        # Partial update
                        did = payload["id"]
                        if did in devices_map:
                            devices_map[did].update(payload)
                            devices_updated = True
                    
                    await broadcast({
                        "type": "mqtt",
                        "topic": topic,
                        "payload": payload,
                        "devices_updated": devices_updated,
                        "devices": devices_map if devices_updated else None
                    })
                    
        except aiomqtt.MqttError as err:
            logger.warning(f"MQTT connection failed: {err}. Retrying in {reconnect_delay} seconds...")
            mqtt_client = None
            await asyncio.sleep(reconnect_delay)
        except Exception as e:
            logger.error(f"Unexpected MQTT error: {e}")
            mqtt_client = None
            await asyncio.sleep(reconnect_delay)

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    task = asyncio.create_task(mqtt_listener())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# Setup Templates & Static
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

@app.get("/")
async def get_index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_connections.add(websocket)
    try:
        # Create init payload
        cloud_devices = {}
        user_code = ""
        
        if CLOUD_PATH.exists():
            try:
                with CLOUD_PATH.open() as f:
                    data = json.load(f)
                    dl = data if isinstance(data, list) else data.values()
                    cloud_devices = {d.get("id"): d for d in dl if "id" in d}
            except Exception as e:
                logger.error(f"Error loading cloud file: {e}")
                
        if CREDS_PATH.exists():
            try:
                with CREDS_PATH.open() as f:
                    creds = json.load(f)
                    user_code = creds.get("user_code", "")
            except Exception as e:
                logger.error(f"Error loading creds file: {e}")

        await websocket.send_text(json.dumps({"type": "init", "devices": devices_map, "cloud_devices": cloud_devices, "user_code": user_code}))
        while True:
            data = await websocket.receive_text()
            cmd = json.loads(data)
            
            # If UI asks us to publish a command to MQTT
            if cmd.get("action") in ["add", "remove", "status", "query", "get", "delete"]:
                action = cmd["action"]
                # In old UI, "delete" payload was sent to publish_action("remove")
                if action == "delete": action = "remove"
                
                payload = cmd.get("payload", {})
                payload["action"] = action
                
                if mqtt_client:
                    pub_topic = mqtt_command_topic.replace("{action}", action).replace("{root}", root_topic)
                    await mqtt_client.publish(pub_topic, json.dumps(payload))
            elif cmd.get("action") == "wizard_start":
                user_code = cmd.get("payload", {}).get("user_code", "")
                asyncio.create_task(run_wizard(user_code))

    except WebSocketDisconnect:
        websocket_connections.remove(websocket)
    except Exception as e:
        logger.error(f"WS error: {e}")
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)

async def run_wizard(user_code: str):
    from tuyawizard.wizard import TuyaWizard, postprocess_devices
    
    wizard_status = {"running": True, "step": "Starting API Login...", "url": None}
    await broadcast({"type": "wizard", "status": wizard_status})

    loop = asyncio.get_running_loop()

    def qr_callback(url):
        wizard_status["url"] = url
        wizard_status["step"] = "Waiting for app scan..." if url else "Fetching devices..."
        asyncio.run_coroutine_threadsafe(broadcast({"type": "wizard", "status": wizard_status}), loop)

    try:
        tuya = TuyaWizard(info_file=str(CREDS_PATH))
        await asyncio.to_thread(tuya.login_auto, user_code=user_code, qr_callback=qr_callback)
        
        wizard_status["step"] = "Fetching devices..."
        wizard_status["url"] = None
        await broadcast({"type": "wizard", "status": wizard_status})
        
        tuyadevices = await asyncio.to_thread(tuya.fetch_devices)
        
        wizard_status["step"] = "Applying post-process (matching subdevices and scanning IPs)..."
        await broadcast({"type": "wizard", "status": wizard_status})
        
        # This function updates tuyadevices in place
        await asyncio.to_thread(postprocess_devices, tuyadevices, "all")
        
        wizard_status["step"] = "Saving devices..."
        await broadcast({"type": "wizard", "status": wizard_status})
        
        with open(CLOUD_PATH, "w", encoding="utf-8") as f:
            json.dump(tuyadevices, f, indent=4, ensure_ascii=False)
            
        wizard_status["step"] = "Wizard Complete! Refreshing devices..."
        wizard_status["running"] = False
        await broadcast({"type": "wizard", "status": wizard_status})
        
    except Exception as e:
        logger.error(f"Wizard error: {e}")
        wizard_status["running"] = False
        wizard_status["error"] = str(e)
        await broadcast({"type": "wizard", "status": wizard_status})
