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
import contextlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import catalog as plugin_catalog
from .cloud import CloudFormatError, parse_cloud_json, save_cloud_json
from .models import Device
from .mqtt import BridgeClient
from .plugins import (
    PLUGIN_API_VERSION,
    PluginContext,
    PluginRegistry,
    ServiceSupervisor,
    discover_plugins,
)
from .scan import LanScanCoordinator
from .state import State
from .wizard import WizardManager

_PKG_ROOT = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_ROOT / "templates"
_STATIC_DIR = _PKG_ROOT / "static"

logger = logging.getLogger(__name__)

# Process-unique id, regenerated every time this module is imported — i.e. once
# per manager process, including after a re-exec (POST /api/restart) or a
# container restart, both of which start a fresh interpreter. Rides the WS
# snapshot so the client can tell a transient reconnect (same id) from a real
# restart (new id) and reload itself on the latter — the tab bar is built once
# at page load and isn't rebuilt on reconnect alone, so a restart that adds or
# removes a plugin would otherwise need a manual F5. PID is unsuitable here:
# os.execvp keeps the same PID, so a re-exec would look unchanged.
_BOOT_ID = uuid.uuid4().hex


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
    snapshot: dict[str, Any] = {
        "version": state.version,
        # Per-process id; a change across a reconnect tells the client the
        # manager restarted, so it reloads to pick up new/removed plugin tabs.
        "boot_id": _BOOT_ID,
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
        # Bridge-reported diagnostics from the latest paginated `status` reply.
        # device_count is the authoritative total; mqtt_drop_count is cumulative
        # publish drops. Surfaced in the "Bridge info" drawer.
        "device_count": state.device_count,
        "mqtt_drop_count": state.mqtt_drop_count,
        # How the bridge is sourced, for the "Bridge info" drawer. "embedded" =
        # spawned in-process via --embed-bridge; "external" = a separate bridge
        # over MQTT. `embed_requested` lets the UI flag the conflict case
        # (embed asked for, but an external bridge already owned the root so the
        # embed was aborted — also carried as the `embedded_bridge_aborted`
        # warning above).
        "bridge_mode": "embedded" if state.bridge_embedded else "external",
        "embed_requested": state.embed_requested,
        # Running bridge build, published into {root}/bridge/config since bridge
        # 0.2.0rc25 (None when an older bridge omits it). Same debug drawer.
        "bridge_version": (state.bridge_config_raw or {}).get("version"),
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
    # Plugin state slices ride the same snapshot, but only once a plugin has
    # actually written one. With no plugins the key is absent entirely so the
    # wire format is byte-identical to a plugin-less build.
    if state._plugins:
        snapshot["plugins"] = dict(state._plugins)
    return snapshot


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


def _reexec_process() -> None:
    """Replace the current process image with a fresh manager (same PID), via
    `os.execvp` on the original argv. This is the "full reload": a brand-new
    Python process re-imports everything, so edited plugin code and removed
    plugins are picked up — and an embedded bridge is respawned — without a
    container/service restart. WebSocket clients drop and auto-reconnect once
    the new process is serving.

    Works wherever the manager was started by its argv: the console script
    (`rustuya-manager …`, incl. the Docker entrypoint) or an absolute path.
    On any failure we exit non-zero so a supervisor (Docker `restart:`, systemd)
    brings us back rather than leaving a half-dead process."""
    logger.warning("re-exec'ing the manager process (full reload): %s", sys.argv)
    try:
        os.execvp(sys.argv[0], sys.argv)
    except Exception:  # noqa: BLE001 - last resort: let a supervisor restart us
        logger.exception("re-exec failed; exiting so a supervisor can restart us")
        os._exit(1)


def build_app(
    state: State,
    client: BridgeClient,
    *,
    creds_path: str | None = None,
    auth: str | None = None,
    plugins: list[Any] | None = None,
    plugin_dirs: list[str] | None = None,
    managed_plugin_dir: str | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> Any:
        # Plugin services (ctx.add_service) start after the app comes up — by
        # which point cli.run has already awaited bootstrap — and are cancelled
        # + awaited on shutdown so none is orphaned. A no-op when no plugin
        # registered a service, so a plugin-less manager is unaffected.
        supervisor = getattr(app.state, "service_supervisor", None)
        if supervisor is not None:
            await supervisor.start()
        try:
            yield
        finally:
            if supervisor is not None:
                await supervisor.stop()

    app = FastAPI(title="rustuya-manager", version=__version__, lifespan=_lifespan)
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
                # Shield so a teardown-time cancellation of the handler
                # itself doesn't interrupt the gather mid-flight, which
                # surfaced as flaky `concurrent.futures.CancelledError`
                # in TestClient runs (the threadpool future the test
                # harness awaits saw an unhandled cancellation chain).
                try:
                    await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
                except asyncio.CancelledError:
                    pass
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

    # ── Plugin host ──────────────────────────────────────────────────────
    # Discover + register plugins at app build time, and again on demand via
    # POST /api/plugins/scan (add-only). With nothing installed this is a no-op:
    # no routers, no pages, no static mounts, and GET /api/plugins returns empty
    # lists. The host is HA-agnostic — it never imports any specific plugin.
    registry = PluginRegistry()
    ctx = PluginContext(registry, bridge_client=client, state=state)
    # Dedup state so a rescan only wires *new* plugins: register callables
    # already run, router objects already included, and plugin ids already
    # mounted. Routes/mounts can be added to a live Starlette app but not cleanly
    # removed, so scan is strictly additive — picking up edited code or removing
    # a plugin needs a full reload (POST /api/restart → _reexec_process).
    _applied: set[Any] = set()
    _included: set[int] = set()
    _mounted: set[str] = set()

    # `Cache-Control: no-cache` forces every load to revalidate against the
    # server's ETag / Last-Modified — a 304 when nothing changed, fresh bytes
    # when it did. Applied to *both* the manager's own /static and each plugin
    # mount below: a plugin's ES module is imported by a bare `/plugins/<id>/
    # index.js` URL that never changes across releases, so without this a
    # drop-in plugin edited on disk (swap files + restart) would keep running
    # the browser's stale cached module — the "did my fix actually deploy?" trap.
    _NO_CACHE = {"cache-control": "no-cache, must-revalidate"}

    class _NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers.update(_NO_CACHE)
            return response

    def _apply_new_plugins(registers: list[Any]) -> int:
        """Register + wire any not-yet-applied plugins; return how many ran."""
        added = 0
        for reg in registers:
            if reg in _applied:
                continue
            _applied.add(reg)
            try:
                reg(ctx)
                added += 1
            except Exception:  # noqa: BLE001 - one bad plugin must not abort the rest
                logger.exception("plugin register(ctx) raised; skipping that plugin")
        for router in registry.api_routers:
            if id(router) in _included:
                continue
            _included.add(id(router))
            app.include_router(router)
        # Pages + init scripts share one per-id static mount under /plugins/{id}.
        seen: dict[str, Path] = {}
        for item in (*registry.pages, *registry.init_scripts):
            seen.setdefault(item["id"], Path(item["static_dir"]))
        for plugin_id, static_dir in seen.items():
            if plugin_id in _mounted:
                continue
            if static_dir.is_dir():
                _mounted.add(plugin_id)
                app.mount(
                    f"/plugins/{plugin_id}",
                    _NoCacheStaticFiles(directory=static_dir),
                    name=f"plugin-{plugin_id}",
                )
            else:
                logger.warning(
                    "plugin %r static_dir %s is not a directory; assets unavailable",
                    plugin_id,
                    static_dir,
                )
        return added

    def _scan_dirs() -> list[str]:
        """Existing directories to scan for drop-in plugins: the managed install
        dir (where the catalog drops things) plus any explicit `plugin_dirs`.
        Non-existent dirs are filtered out so an as-yet-uncreated managed dir
        doesn't log a spurious 'not a directory' warning every boot."""
        candidates = [managed_plugin_dir, *(plugin_dirs or [])]
        return [d for d in candidates if d and Path(d).is_dir()]

    def _discover(register_callables: list[Any] | None = None) -> list[Any]:
        """discover_plugins over the live scan dirs, skipping packages the ledger
        marks disabled. The ledger is read fresh each call so a disable/enable
        takes effect on the next restart's discovery without restarting twice."""
        return discover_plugins(
            register_callables=register_callables,
            plugin_dirs=_scan_dirs(),
            skip_packages=plugin_catalog.disabled_packages(managed_plugin_dir),
        )

    _apply_new_plugins(_discover(register_callables=plugins))

    # Supervisor for any long-lived plugin services (ctx.add_service). Created
    # now that build-time plugins have registered; started/stopped by the
    # lifespan above. Holds the registry by reference, so it reads `services`
    # at startup time (covering anything the initial apply registered).
    app.state.service_supervisor = ServiceSupervisor(registry)

    def _plugin_manifest() -> dict[str, Any]:
        return {
            "pages": [
                {
                    "id": page["id"],
                    "label": page["label"],
                    "js_url": f"/plugins/{page['id']}/{page['entry']}",
                }
                for page in registry.pages
            ],
            "init_scripts": [f"/plugins/{s['id']}/{s['entry']}" for s in registry.init_scripts],
        }

    @app.get("/api/plugins")
    async def get_plugins() -> dict[str, Any]:
        """Manifest the frontend boot fetches:

            {
              "pages": [{id, label, js_url}],        # lazy tab pages
              "init_scripts": ["/plugins/<id>/init.js", ...]  # eager modules
            }

        Both lists empty ⇒ plugin-less UI is identical to today (no tab bar, no
        eager imports). `init_scripts` modules export `init(ctx)` and run at
        boot — that's how a plugin contributes always-visible UI such as a
        hamburger-menu item (ctx.addHeaderAction)."""
        return _plugin_manifest()

    @app.post("/api/plugins/scan")
    async def scan_plugins() -> dict[str, Any]:
        """Load plugins newly dropped into the plugin dir, without a restart.
        Add-only: returns `{ok, added, pages, init_scripts}`. Cannot reload
        edited plugin code or unload a plugin — that needs POST /api/restart."""
        added = _apply_new_plugins(_discover())
        logger.info("plugin scan: %d new plugin(s) loaded", added)
        return {"ok": True, "added": added, **_plugin_manifest()}

    @app.get("/api/plugins/catalog")
    async def get_plugin_catalog() -> dict[str, Any]:
        """The curated install catalog, each entry annotated with install state.

        Drives the host-owned "Manage plugins" UI. `api_version` lets the UI grey
        out entries whose `min_api` exceeds this host. `managed` is False when
        there's no writable install dir, so the UI can explain why Install is
        unavailable. Reads the on-disk ledger fresh each call so state reflects
        installs/uninstalls done since boot."""
        ledger = plugin_catalog.read_ledger(managed_plugin_dir)
        return {
            "api_version": PLUGIN_API_VERSION,
            "managed": managed_plugin_dir is not None,
            "plugins": plugin_catalog.annotate_catalog(
                plugin_catalog.load_bundled_catalog(), ledger
            ),
        }

    @app.post("/api/plugins/install")
    async def install_plugin_endpoint(request: Request) -> dict[str, Any]:
        """Install a catalog plugin by id into the managed dir, then wire it live.

        Body: `{"id": "<catalog id>"}`. The flow is: validate (managed dir
        present, id known, min_api compatible, not already installed) → download
        + sha256-verify + unpack off the event loop → existing add-only scan. A
        fresh install needs no restart (routes/pages/mounts are only ever added),
        so the new tab appears on the next manifest fetch. Update/uninstall —
        which must drop already-imported code — are separate, restart-required
        endpoints."""
        if managed_plugin_dir is None:
            raise HTTPException(400, "no managed plugin directory; install is unavailable")
        body = await request.json()
        plugin_id = body.get("id") if isinstance(body, dict) else None
        if not plugin_id:
            raise HTTPException(400, "missing plugin id")
        entry = next(
            (e for e in plugin_catalog.load_bundled_catalog() if e["id"] == plugin_id),
            None,
        )
        if entry is None:
            raise HTTPException(404, f"unknown plugin id {plugin_id!r}")
        if entry.get("min_api", 1) > PLUGIN_API_VERSION:
            raise HTTPException(
                409,
                f"{plugin_id} requires plugin API v{entry['min_api']}; "
                f"this manager provides v{PLUGIN_API_VERSION} — upgrade the manager",
            )
        if plugin_id in plugin_catalog.read_ledger(managed_plugin_dir):
            raise HTTPException(409, f"{plugin_id} is already installed")
        try:
            record = await asyncio.to_thread(
                plugin_catalog.install_plugin, entry, managed_plugin_dir
            )
        except plugin_catalog.CatalogError as exc:
            raise HTTPException(400, str(exc)) from exc
        added = _apply_new_plugins(_discover())
        logger.info("installed plugin %r; %d new plugin(s) wired", plugin_id, added)
        return {
            "ok": True,
            "id": plugin_id,
            "installed_version": record["version"],
            "added": added,
            **_plugin_manifest(),
        }

    def _require_managed() -> None:
        if managed_plugin_dir is None:
            raise HTTPException(400, "no managed plugin directory; management is unavailable")

    async def _read_id(request: Request) -> str:
        body = await request.json()
        plugin_id = body.get("id") if isinstance(body, dict) else None
        if not plugin_id:
            raise HTTPException(400, "missing plugin id")
        return plugin_id

    @app.post("/api/plugins/update")
    async def update_plugin_endpoint(request: Request) -> dict[str, Any]:
        """Re-install an installed catalog plugin at the catalog's current version.

        Unlike install, an update can't take effect live — the old module is
        already imported and its routes/pages/mounts can't be cleanly swapped —
        so the files + ledger are replaced on disk and the response carries
        `restart_required: true` for the UI to act on (POST /api/restart)."""
        _require_managed()
        plugin_id = await _read_id(request)
        entry = next(
            (e for e in plugin_catalog.load_bundled_catalog() if e["id"] == plugin_id), None
        )
        if entry is None:
            raise HTTPException(404, f"unknown plugin id {plugin_id!r}")
        if plugin_id not in plugin_catalog.read_ledger(managed_plugin_dir):
            raise HTTPException(409, f"{plugin_id} is not installed")
        if entry.get("min_api", 1) > PLUGIN_API_VERSION:
            raise HTTPException(409, f"{plugin_id} requires a newer manager")
        try:
            record = await asyncio.to_thread(
                plugin_catalog.install_plugin, entry, managed_plugin_dir, replace=True
            )
        except plugin_catalog.CatalogError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "ok": True,
            "id": plugin_id,
            "installed_version": record["version"],
            "restart_required": True,
        }

    @app.post("/api/plugins/uninstall")
    async def uninstall_plugin_endpoint(request: Request) -> dict[str, Any]:
        """Remove an installed catalog plugin's files and ledger entry.

        Only catalog/drop-in plugins in the managed dir can be uninstalled this
        way; pip-installed entry-point plugins aren't tracked here. The running
        process keeps the old module until restart, so `restart_required: true`."""
        _require_managed()
        plugin_id = await _read_id(request)
        if plugin_id not in plugin_catalog.read_ledger(managed_plugin_dir):
            raise HTTPException(404, f"{plugin_id} is not installed")
        await asyncio.to_thread(plugin_catalog.uninstall_plugin, plugin_id, managed_plugin_dir)
        logger.info("uninstalled plugin %r", plugin_id)
        return {"ok": True, "id": plugin_id, "restart_required": True}

    @app.post("/api/plugins/toggle")
    async def toggle_plugin_endpoint(request: Request) -> dict[str, Any]:
        """Enable/disable an installed plugin without removing it.

        Body: `{"id": ..., "enabled": bool}`. Sets the ledger's `disabled` flag;
        discovery honours it on the next scan/restart. A live plugin keeps
        running until then, and a re-enabled one isn't loaded until then either,
        so `restart_required: true`."""
        _require_managed()
        body = await request.json()
        plugin_id = body.get("id") if isinstance(body, dict) else None
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not plugin_id or not isinstance(enabled, bool):
            raise HTTPException(400, "body must be {id: str, enabled: bool}")
        try:
            plugin_catalog.set_disabled(plugin_id, managed_plugin_dir, not enabled)
        except plugin_catalog.CatalogError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "id": plugin_id, "enabled": enabled, "restart_required": True}

    # Restart hook is indirected through app.state so tests can stub it (the real
    # one replaces the process). Scheduled a beat after responding so the HTTP
    # 200 reaches the client before the image is swapped.
    app.state.restart_hook = _reexec_process
    app.state.restart_delay = 0.5

    @app.post("/api/restart")
    async def restart_manager() -> dict[str, Any]:
        """Restart the manager process in place (full reload). Picks up edited
        plugin code, drops removed plugins, respawns an embedded bridge —
        lighter than a container restart and works outside Docker too. Clients'
        WebSockets drop and auto-reconnect once the new process is serving."""
        logger.warning("manager restart requested via web UI")
        loop = asyncio.get_running_loop()
        loop.call_later(app.state.restart_delay, app.state.restart_hook)
        return {"ok": True}

    @app.get("/api/locales")
    async def list_locales() -> dict[str, Any]:
        """Enumerate the UI translation catalogs bundled under static/locales/.

        The web client uses this to populate its language picker, so dropping a
        new `xx.json` next to en.json/ko.json makes "xx" selectable with no code
        change. Each locale's own `lang.name` key is surfaced in `names` so the
        picker can show native names (English / 한국어 / 日本語) without the client
        fetching every catalog. English is always offered (and is the client's
        fallback layer), even if the directory read fails for some reason."""
        locales_dir = _STATIC_DIR / "locales"
        codes = {"en"}
        names: dict[str, str] = {}
        try:
            for f in locales_dir.glob("*.json"):
                if not f.is_file():
                    continue
                codes.add(f.stem)
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    name = data.get("lang.name")
                    if isinstance(name, str) and name:
                        names[f.stem] = name
                except (OSError, ValueError) as e:
                    logger.warning("could not read lang.name from %s: %s", f, e)
        except OSError as e:
            logger.warning("could not enumerate locales dir %s: %s", locales_dir, e)
        return {"available": sorted(codes), "default": "en", "names": names}

    # Static assets (JS, eventual CSS, icons). Tailwind comes from a CDN inside
    # the HTML, so there's no build step. Served no-cache (see _NoCacheStaticFiles
    # above) so every release revalidates instead of pulling stale ES-module
    # siblings (bare `./state.js` imports) out of disk cache.
    if _STATIC_DIR.is_dir():
        app.mount("/static", _NoCacheStaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        index = _TEMPLATES_DIR / "index.html"
        if not index.exists():
            raise HTTPException(500, "index.html missing from package")
        return FileResponse(index, media_type="text/html", headers=_NO_CACHE)

    return app
