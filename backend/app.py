import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import aiomqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rustuya-manager")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent))
CLOUD_PATH = DATA_DIR / "tuyadevices.json"
CREDS_PATH = DATA_DIR / "tuyacreds.json"

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_ROOT_TOPIC = os.getenv("MQTT_ROOT_TOPIC", "rustuya")

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
class AppState:
    def __init__(self):
        self.websocket_connections: set[WebSocket] = set()
        self.background_tasks: set[asyncio.Task] = set()
        self.mqtt_client: aiomqtt.Client | None = None
        self.mqtt_connected: bool = False

state = AppState()

# ---------------------------------------------------------------------------
# WebSocket & Helpers
# ---------------------------------------------------------------------------
async def broadcast(message: dict) -> None:
    if state.websocket_connections:
        payload = json.dumps(message)
        dead_ws = set()
        for ws in state.websocket_connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead_ws.add(ws)
        state.websocket_connections -= dead_ws

async def load_init_data() -> dict:
    try:
        raw = json.loads(CLOUD_PATH.read_text(encoding="utf-8")) if CLOUD_PATH.exists() else []
        items = raw if isinstance(raw, list) else list(raw.values())
        cloud_devices = {d["id"]: d for d in items if isinstance(d, dict) and "id" in d}
    except Exception as e:
        logger.error(f"Error reading cloud path: {e}")
        cloud_devices = {}

    try:
        creds = json.loads(CREDS_PATH.read_text(encoding="utf-8")) if CREDS_PATH.exists() else {}
        user_code = creds.get("user_code", "") if isinstance(creds, dict) else ""
    except Exception as e:
        logger.error(f"Error reading creds path: {e}")
        user_code = ""

    return {"cloud_devices": cloud_devices, "user_code": user_code}

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
# MQTT Listener
# ---------------------------------------------------------------------------
async def mqtt_listener() -> None:
    reconnect_delay = 5
    while True:
        try:
            async with aiomqtt.Client(
                hostname=MQTT_BROKER, 
                port=MQTT_PORT,
                username=MQTT_USER,
                password=MQTT_PASSWORD
            ) as client:
                state.mqtt_client = client
                state.mqtt_connected = True
                reconnect_delay = 5
                logger.info("MQTT Proxy connected to %s:%d", MQTT_BROKER, MQTT_PORT)
                await broadcast({"type": "mqtt_status", "connected": True})

                await client.subscribe(f"{MQTT_ROOT_TOPIC}/#")

                async for message in client.messages:
                    topic = str(message.topic)
                    try:
                        payload = json.loads(message.payload.decode())
                    except Exception:
                        payload = message.payload.decode()

                    await broadcast({
                        "type": "mqtt_message",
                        "topic": topic,
                        "payload": payload
                    })

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("MQTT Error: %s", e)
            state.mqtt_client = None
            state.mqtt_connected = False
            await broadcast({"type": "mqtt_status", "connected": False})
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(mqtt_listener())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

# Static files (React build)
STATIC_DIR = BASE_DIR / "static"
if not STATIC_DIR.exists():
    # If static dir doesn't exist, just serve empty or a message (useful during dev)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets"), check_dir=False), name="assets")

@app.get("/")
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Frontend not built yet. Run 'npm run build' in frontend directory and copy to backend/static."}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.websocket_connections.add(websocket)
    try:
        init_data = await load_init_data()
        await websocket.send_text(json.dumps({
            "type": "init",
            "mqtt_connected": state.mqtt_connected,
            **init_data,
        }))
        
        async for raw in websocket.iter_text():
            cmd = json.loads(raw)
            action = cmd.get("action")
            
            if action == "wizard_start":
                payload = cmd.get("payload", {})
                task = asyncio.create_task(run_wizard(payload.get("user_code", "")))
                state.background_tasks.add(task)
                task.add_done_callback(state.background_tasks.discard)
            elif action == "mqtt_publish":
                # Frontend requests us to publish to MQTT
                if state.mqtt_client:
                    topic = cmd.get("topic")
                    payload = cmd.get("payload")
                    if isinstance(payload, dict):
                        payload_str = json.dumps(payload)
                    else:
                        payload_str = str(payload)
                    await state.mqtt_client.publish(topic, payload_str)
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "MQTT not connected"
                    }))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS error: %s", e)
    finally:
        state.websocket_connections.discard(websocket)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8373))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
