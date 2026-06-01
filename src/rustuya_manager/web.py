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
import base64
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cloud import CloudFormatError, parse_cloud_json, save_cloud_json
from .models import Device
from .mqtt import BridgeClient
from .scan import LanScanCoordinator
from .state import State
from .wizard import WizardManager

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
    # Without a cloud, every bridge device would appear as "orphaned" in the
    # raw diff — mathematically correct but semantically meaningless and
    # confusing in the UI (the filter tab shows N orphans but the cards are
    # rendered as "ungrouped"). Suppress the diff arrays in that case so the
    # presentation matches what `classifyDevice` in the client computes.
    cloud_loaded = bool(state.cloud)
    if cloud_loaded:
        diff = state.diff()
        diff_payload = {
            "synced": [d.id for d in diff.synced],
            "mismatched": [{"id": d.id, "reasons": reasons} for d, reasons in diff.mismatched],
            "missing": [d.id for d in diff.missing],
            "orphaned": [d.id for d in diff.orphaned],
        }
    else:
        diff_payload = {"synced": [], "mismatched": [], "missing": [], "orphaned": []}
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
        "diff": diff_payload,
        "dps": state.dps,
        "last_response": state.last_response,
        "last_seen": state.last_seen,
        "retained_only": sorted(state.retained_only),
        "live_status": state.live_status,
        "warnings": state.warnings,
        "cloud_path": state.cloud_path,
        "cloud_loaded": bool(state.cloud),
        # Wholesale dict (id → sighting) from the last LAN scan. Empty
        # until the first scan completes. Dataclass → dict here keeps
        # the WS frame schema-stable for the UI (no datetime/None
        # surprises from dataclasses.asdict).
        "scan_results": {
            sid: {
                "id": s.id,
                "ip": s.ip,
                "version": s.version,
                "observed_at": s.observed_at,
            }
            for sid, s in state.scan_results.items()
        },
    }


class _BasicAuthMiddleware:
    """ASGI middleware that gates HTTP + WebSocket on a single Basic credential.

    Lives at the ASGI level rather than as a FastAPI dependency because the
    WebSocket upgrade handshake doesn't pass through dependency-injection —
    the WS scope still carries the `authorization` header from the browser,
    so a shared middleware can authenticate both surfaces uniformly.

    Browser flow: hitting any page with no creds returns 401 +
    WWW-Authenticate, the browser prompts and caches the credentials for the
    origin, and every subsequent request (including the /ws upgrade) carries
    them automatically. No cookie or session storage needed.
    """

    def __init__(self, app, expected_header: bytes) -> None:
        self.app = app
        self.expected_header = expected_header

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization") != self.expected_header:
                if scope["type"] == "http":
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [
                                (b"www-authenticate", b'Basic realm="rustuya-manager"'),
                                (b"content-type", b"text/plain"),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": b"Unauthorized\n"})
                else:
                    # 1008 = policy violation, the closest WS code to "auth failure".
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


def build_app(
    state: State,
    client: BridgeClient,
    *,
    creds_path: str | None = None,
    auth: str | None = None,
) -> FastAPI:
    app = FastAPI(title="rustuya-manager", version="0.1.0rc19")
    if auth:
        if ":" not in auth:
            raise ValueError("--auth must be in 'user:password' form")
        expected = b"Basic " + base64.b64encode(auth.encode("utf-8"))
        # add_middleware wraps via ASGI dispatch, which is what we want here
        # (FastAPI's HTTPBasic dependency wouldn't cover WebSocket upgrades).
        app.add_middleware(_BasicAuthMiddleware, expected_header=expected)
    # Hold client/state on app so dependency-injection or middleware can reach them
    app.state.bridge_state = state
    app.state.bridge_client = client

    # Tuya cloud login wizard. When devices come back from tuyawizard, write
    # them through cloud.py so state.cloud is populated identically to a JSON
    # upload, and persist to disk if a cloud_path is known.
    async def _on_wizard_devices(devices: list[dict[str, Any]]) -> None:
        import json as _json

        raw = _json.dumps(devices, ensure_ascii=False)
        try:
            parsed = parse_cloud_json(raw)
        except CloudFormatError as e:
            logger.warning("wizard returned unparseable device shape: %s", e)
            return
        await state.set_cloud(parsed)
        if state.cloud_path:
            try:
                save_cloud_json(raw, Path(state.cloud_path))
            except OSError as e:
                logger.warning("wizard fetched devices but persist failed: %s", e)

    # Single coordinator shared between the wizard (bakes scan results into
    # cloud devices) and the Scan button (surfaces sightings to the UI).
    # See scan.py for the single-flight rationale.
    scan_coordinator = LanScanCoordinator(client, state)
    app.state.scan_coordinator = scan_coordinator

    wizard_creds = creds_path or "tuyacreds.json"
    wizard = WizardManager(
        creds_path=wizard_creds,
        on_devices=_on_wizard_devices,
        scan_coordinator=scan_coordinator,
    )
    app.state.wizard = wizard

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return serialize_state(state)

    @app.post("/api/wizard/start")
    async def wizard_start(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Kick off the QR login flow. `user_code` is the Tuya account ID
        retrieved from Smart Life → Me → Settings → Account and Security.
        `scan` toggles the post-fetch UDP scan that bakes a current LAN IP
        into each device record — off by default so DHCP changes don't
        silently break bridge connectivity."""
        body = body if isinstance(body, dict) else {}
        user_code = body.get("user_code") or None
        scan = bool(body.get("scan"))
        session = await wizard.start(user_code=user_code, scan=scan)
        return session.to_dict()

    @app.get("/api/wizard/status")
    async def wizard_status() -> dict[str, Any]:
        return wizard.session.to_dict()

    @app.get("/api/wizard/info")
    async def wizard_info() -> dict[str, Any]:
        """One-shot endpoint called when the modal opens. Returns the
        user_code persisted in tuyacreds.json (if any) so the input can be
        pre-filled across browsers. Kept separate from /status because that
        gets polled every 1.5s during the flow — we don't want to re-read
        the file on every tick."""
        return {"saved_user_code": wizard.read_saved_user_code() or ""}

    @app.post("/api/wizard/cancel")
    async def wizard_cancel() -> dict[str, Any]:
        await wizard.cancel()
        return wizard.session.to_dict()

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
        extra = {k: v for k, v in body.items() if k not in ("action", "id", "name")}
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

    @app.post("/api/scan")
    async def post_scan() -> dict[str, Any]:
        """Run a bridge LAN scan and cache the sightings on state.

        Returns `{ok, count}`; the per-device sighting data lands on the
        WebSocket snapshot (`scan_results`) once the run completes, so
        the UI auto-refreshes without needing the response body. A 503
        is returned when the broker is disconnected — `publish_command`
        is the surface that distinguishes that from a "scan ran but no
        device replied" (which is a healthy `count == 0`).
        """
        try:
            sightings = await scan_coordinator.run()
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from None
        return {"ok": True, "count": len(sightings)}

    @app.websocket("/ws")
    async def ws_state(ws: WebSocket) -> None:
        await ws.accept()
        try:
            # Send initial snapshot so the client doesn't need a separate GET
            await ws.send_json(serialize_state(state))
            last_seen = state.version
            while True:
                # Race the state-change wait against ws.receive() so a client
                # disconnect aborts this handler promptly. Without the race,
                # `state.wait_for_change` would block until the NEXT state
                # change — and Starlette doesn't auto-cancel WS handler tasks
                # on disconnect — so a closed client would leave the task
                # parked indefinitely. Each parked task retains the WS object,
                # its frame locals, and a slot in the Condition's waiter
                # deque; over many connection cycles (browser refreshes,
                # tab reopens) that retention grew linearly at ~160 KB per
                # cycle. The race converts disconnect into a prompt exit so
                # the task graph can be reclaimed.
                change_task = asyncio.create_task(state.wait_for_change(last_seen))
                recv_task = asyncio.create_task(ws.receive())
                done, pending = await asyncio.wait(
                    {change_task, recv_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Drain cancellations so their CancelledError doesn't leak
                # into the event loop as an unhandled-exception warning.
                await asyncio.gather(*pending, return_exceptions=True)
                if recv_task in done:
                    # The JS client never sends messages on this socket, so
                    # any completion here means disconnect (or a stray frame
                    # we ignore by exiting). Returning closes the handler;
                    # Starlette closes the WS for us.
                    return
                last_seen = change_task.result()
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
    #
    # `Cache-Control: no-cache` forces every load to revalidate against the
    # server's ETag / Last-Modified — when nothing changed, Starlette returns
    # 304 with no body, so the perceived cost is negligible. The alternative
    # (default heuristic caching) bites every release: ES-module imports use
    # bare paths like `./state.js`, so a `?v=` busted root script.js still
    # pulls stale siblings out of disk cache. Revalidation skips that whole
    # class of "did my fix actually deploy?" confusion.
    _NO_CACHE = {"cache-control": "no-cache, must-revalidate"}
    if _STATIC_DIR.is_dir():

        class _NoCacheStaticFiles(StaticFiles):
            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                response.headers.update(_NO_CACHE)
                return response

        app.mount("/static", _NoCacheStaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        index = _TEMPLATES_DIR / "index.html"
        if not index.exists():
            raise HTTPException(500, "index.html missing from package")
        return FileResponse(index, media_type="text/html", headers=_NO_CACHE)

    return app
