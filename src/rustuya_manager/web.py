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
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .mqtt import BridgeClient
from .models import Device
from .state import State

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
    }


def build_app(state: State, client: BridgeClient) -> FastAPI:
    app = FastAPI(title="rustuya-manager", version="0.2.0")
    # Hold client/state on app so dependency-injection or middleware can reach them
    app.state.bridge_state = state
    app.state.bridge_client = client

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return serialize_state(state)

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

    @app.get("/")
    async def root() -> JSONResponse:
        # Step-4 will replace this with a real HTML page. For now, return a
        # tiny pointer so curl users aren't greeted with 404.
        return JSONResponse(
            {
                "service": "rustuya-manager",
                "endpoints": {
                    "state": "/api/state",
                    "command": "POST /api/command",
                    "ws": "/ws",
                },
            }
        )

    return app
