"""FastAPI app + WebSocket broadcaster on top of the Step-2 backend.

Endpoints:
    GET  /api/state    JSON snapshot of cloud/bridge/diff/dps + resolved templates
    POST /api/command  publish a command via the BridgeClient (used by Step-4 UI)
    WS   /ws           pushes the JSON snapshot every time State mutates

The web layer owns no state of its own; the running BridgeClient feeds the
shared `State` object and the WebSocket loop watches `state.version` to
broadcast deltas. The same State drives the CLI dashboard, so REST and CLI
never disagree.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cloud import CloudFormatError, parse_cloud_json, save_cloud_json
from .mqtt import BridgeClient
from .models import Device
from .state import State

_PKG_ROOT = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_ROOT / "templates"
_STATIC_DIR = _PKG_ROOT / "static"

logger = logging.getLogger(__name__)


def _device_to_dict(d: Device) -> dict[str, Any]:
    # raw_data can be large and is only useful for debugging; omit by default
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "cid": d.cid,
        "parent_id": d.parent_id,
        "key": d.key,
        "ip": d.ip,
        "version": d.version,
        "status": d.status,
    }


def serialize_state(state: State) -> dict[str, Any]:
    """Render the full state as a JSON-safe dict — used by both REST and WS."""
    diff = state.diff()
    tpl = state.templates
    return {
        "version": state.version,
        "templates": (
            {
                "root": tpl.root,
                "command": tpl.command,
                "event": tpl.event,
                "message": tpl.message,
                "scanner": tpl.scanner,
                "payload": tpl.payload,
            }
            if tpl
            else None
        ),
        "cloud": {did: _device_to_dict(d) for did, d in state.cloud.items()},
        "bridge": {did: _device_to_dict(d) for did, d in state.bridge.items()},
        "diff": {
            "synced": [d.id for d in diff.synced],
            "mismatched": [
                {"id": d.id, "reasons": reasons} for d, reasons in diff.mismatched
            ],
            "missing": [d.id for d in diff.missing],
            "orphaned": [d.id for d in diff.orphaned],
        },
        "dps": state.dps,
        "last_response": state.last_response,
        "last_seen": state.last_seen,
        "cloud_path": state.cloud_path,
        "cloud_loaded": bool(state.cloud),
    }


def build_app(state: State, client: BridgeClient) -> FastAPI:
    app = FastAPI(title="rustuya-manager", version="0.2.0")
    # Hold client/state on app so dependency-injection or middleware can reach them
    app.state.bridge_state = state
    app.state.bridge_client = client

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return serialize_state(state)

    @app.post("/api/cloud")
    async def post_cloud(request: Request) -> dict[str, Any]:
        """Accepts a Tuya devices JSON upload and applies it to state.

        The browser sends raw JSON (Content-Type: application/json or text/plain).
        If state already knows a cloud_path, the upload is also persisted there
        so the manager re-loads it on restart."""
        raw = await request.body()
        if not raw:
            raise HTTPException(400, "empty body")
        try:
            devices = parse_cloud_json(raw)
        except CloudFormatError as e:
            raise HTTPException(400, str(e)) from None

        await state.set_cloud(devices)

        persisted_to: str | None = None
        if state.cloud_path:
            try:
                from pathlib import Path

                save_cloud_json(raw, Path(state.cloud_path))
                persisted_to = state.cloud_path
            except OSError as e:
                logger.warning("cloud upload accepted but persist failed: %s", e)

        return {
            "ok": True,
            "count": len(devices),
            "persisted_to": persisted_to,
        }

    @app.post("/api/command")
    async def post_command(body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        if not action or not isinstance(action, str):
            raise HTTPException(400, "missing 'action'")
        target_id = body.get("id")
        target_name = body.get("name")
        extra = {
            k: v
            for k, v in body.items()
            if k not in ("action", "id", "name")
        }
        try:
            await client.publish_command(
                action,
                target_id=target_id,
                target_name=target_name,
                extra=extra or None,
            )
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from None
        return {"ok": True, "published": {"action": action, "id": target_id}}

    @app.websocket("/ws")
    async def ws_state(ws: WebSocket) -> None:
        await ws.accept()
        try:
            # Send initial snapshot so the client doesn't need a separate GET
            await ws.send_json(serialize_state(state))
            last_seen = state.version
            while True:
                last_seen = await state.wait_for_change(last_seen)
                await ws.send_json(serialize_state(state))
        except WebSocketDisconnect:
            return
        except Exception:
            logger.exception("WebSocket loop crashed")
            try:
                await ws.close(code=1011)
            except Exception:
                pass

    # Static assets (JS, eventual CSS, icons). Tailwind comes from a CDN inside
    # the HTML, so there's no build step.
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        index = _TEMPLATES_DIR / "index.html"
        if not index.exists():
            raise HTTPException(500, "index.html missing from package")
        return FileResponse(index, media_type="text/html")

    return app
