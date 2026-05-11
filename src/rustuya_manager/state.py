"""Single source of truth for runtime state shared by MQTT loop and UI.

Holds: cloud device snapshot (loaded once from JSON), bridge device snapshot
(updated whenever the bridge replies to a `status` command or an event flows in),
and the bridge's resolved topic templates.

All mutations go through `update_*` methods so an `asyncio.Event` can wake
listeners. The web UI's WebSocket broadcaster and the CLI's redraw loop both
consume the same change stream.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .diff import DiffResult, diff
from .models import Device


def _now() -> float:
    return time.time()


@dataclass
class BridgeTemplates:
    """Post-`{root}`-substituted templates ready to feed pyrustuyabridge helpers."""

    root: str
    command: str  # for forward substitution + publish
    event: str  # for match_topic on incoming events
    message: str  # for match_topic on response/error replies
    scanner: str  # for match_topic on scan results
    payload: str = "{value}"  # the bridge's user payload template


@dataclass
class State:
    cloud: dict[str, Device] = field(default_factory=dict)
    bridge: dict[str, Device] = field(default_factory=dict)
    templates: BridgeTemplates | None = None
    # Per-device live DPS values keyed by device id.
    dps: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Last action result keyed by device id ("ok", "error", or message text).
    last_response: dict[str, dict[str, Any]] = field(default_factory=dict)
    # UNIX seconds of the most recent event/response observed per device id.
    last_seen: dict[str, float] = field(default_factory=dict)
    # Live online/offline state, surfaced via the bridge's `error` topic and
    # DPS events. Each entry is {"state": "online"|"offline"|"unknown",
    # "code": int|None, "message": str|None}.
    live_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Where the cloud devices JSON was last loaded from (None until set).
    cloud_path: str | None = None

    _version: int = 0
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    @property
    def version(self) -> int:
        return self._version

    def diff(self) -> DiffResult:
        return diff(self.cloud, self.bridge)

    # ── mutators ──────────────────────────────────────────────────────────
    async def set_cloud(self, devices: dict[str, Device]) -> None:
        async with self._changed:
            self.cloud = devices
            self._bump()

    async def set_bridge(self, devices: dict[str, Device]) -> None:
        async with self._changed:
            self.bridge = devices
            self._bump()

    async def set_templates(self, t: BridgeTemplates) -> None:
        async with self._changed:
            self.templates = t
            self._bump()

    async def merge_dps(self, device_id: str, new_dps: dict[str, Any], at: float | None = None) -> None:
        async with self._changed:
            existing = self.dps.setdefault(device_id, {})
            existing.update(new_dps)
            self.last_seen[device_id] = at if at is not None else _now()
            self._bump()

    async def record_response(self, target_id: str, response: dict[str, Any], at: float | None = None) -> None:
        async with self._changed:
            self.last_response[target_id] = response
            self.last_seen[target_id] = at if at is not None else _now()
            self._bump()

    async def set_cloud_path(self, path: str) -> None:
        async with self._changed:
            self.cloud_path = path
            self._bump()

    async def set_live_status(
        self, device_id: str, state: str, code: int | None = None, message: str | None = None
    ) -> None:
        async with self._changed:
            self.live_status[device_id] = {"state": state, "code": code, "message": message}
            self._bump()

    def _bump(self) -> None:
        self._version += 1
        self._changed.notify_all()

    async def wait_for_change(self, since_version: int) -> int:
        """Awaits a mutation past `since_version` and returns the new version.

        Callers pass the version they last observed; this returns once state
        has moved past that point. Works for any number of concurrent waiters."""
        async with self._changed:
            await self._changed.wait_for(lambda: self._version > since_version)
            return self._version
