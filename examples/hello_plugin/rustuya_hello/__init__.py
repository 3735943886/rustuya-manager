"""Example rustuya-manager plugin — exercises all six host surfaces.

This package is intentionally NOT shipped with rustuya-manager (it is not in the
manager's package-data or entry points). Install it into the same environment to
manually prove the host end to end:

    pip install -e examples/hello_plugin
    rustuya-manager --web   # a "Hello" tab now appears

It demonstrates, via the single `register(ctx)` entry point:
  1. an API router          (GET /api/hello/ping)
  2. an MQTT subscription   (hello/#  → updates the state namespace)
  3. a state namespace      ("hello", rides the WS broadcast)
  4. a UI page              (the "Hello" tab + static/index.js)
  5. the bridge client      (ctx.bridge_client, available for publishing)
  6. a header menu item     (eager static/init.js → ctx.addHeaderAction)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def register(ctx) -> None:
    """Called once by the manager's plugin host at app build time."""
    logger.info("hello plugin registering against host API v%s", ctx.api_version)

    ns = ctx.state_namespace("hello")
    # State is async, but register() is sync; seed lazily on first message/API
    # call instead. We expose a small mutable counter via the namespace.
    counter = {"pings": 0, "last_mqtt": None}

    # ── 1. API router ────────────────────────────────────────────────────
    router = APIRouter()

    @router.get("/api/hello/ping")
    async def ping() -> dict:
        counter["pings"] += 1
        await ns.set({"pings": counter["pings"], "last_mqtt": counter["last_mqtt"]})
        return {"pong": True, "pings": counter["pings"]}

    ctx.add_api_router(router)

    # ── 2 + 3. MQTT subscription writing the state namespace ─────────────
    async def on_message(topic: str, payload: str, retain: bool) -> None:
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            parsed = payload
        counter["last_mqtt"] = {"topic": topic, "payload": parsed, "retain": retain}
        await ns.set({"pings": counter["pings"], "last_mqtt": counter["last_mqtt"]})

    ctx.add_mqtt_subscription("hello/#", on_message)

    # ── 4. UI page ───────────────────────────────────────────────────────
    ctx.add_page("hello", "Hello", static_dir=str(_STATIC_DIR), entry="index.js")

    # ── 6. Header menu item (eager init script) ──────────────────────────
    # init.js runs at boot and calls ctx.addHeaderAction, so a "Ping" entry
    # appears in the hamburger menu without opening the Hello tab. Shares the
    # "hello" id/static_dir with the page, so it's served from the same mount.
    ctx.add_header_init("hello", static_dir=str(_STATIC_DIR), entry="init.js")
