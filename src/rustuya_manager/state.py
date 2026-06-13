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


@dataclass(frozen=True)
class ScanSighting:
    """One bridge-side LAN scan observation for a device.

    Frozen so cached results can't be mutated after a coordinator stores
    them on state — the snapshot serializer reads the dict directly.
    `ip`/`version` are optional because the bridge's sighting payload
    only guarantees `id` (LAN-broadcasted device id); the rest depends
    on what the device returned to the scan probe.
    """

    id: str
    ip: str | None
    version: str | None
    observed_at: float  # unix seconds; same epoch as State.last_seen


@dataclass
class State:
    cloud: dict[str, Device] = field(default_factory=dict)
    bridge: dict[str, Device] = field(default_factory=dict)
    templates: BridgeTemplates | None = None
    # The raw, unparsed `{root}/bridge/config` payload dict, exactly as the
    # bridge published it (before `{root}` substitution). `templates` above is
    # the resolved/derived form the manager uses internally, but it drops
    # fields the manager doesn't need — notably `mqtt_retain`. Plugins that
    # want to re-derive their own view of the config (e.g. rustuya-ha building
    # an HA discovery scheme) need the original keys, so we keep the raw dict
    # here and expose it read-only via `PluginContext.bridge_config()`. None
    # until the first retained config is parsed.
    bridge_config_raw: dict[str, Any] | None = None
    # Per-device live DPS values keyed by device id.
    dps: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Last action result keyed by device id ("ok", "error", or message text).
    last_response: dict[str, dict[str, Any]] = field(default_factory=dict)
    # UNIX seconds of the most recent **live** event/response observed per
    # device id. Retained messages don't carry a publish timestamp in MQTT
    # v3.1.1, so stamping last_seen for them would falsely advertise stale
    # data as "just now" — instead we leave last_seen untouched and add the
    # id to `retained_only` (below) so the UI can show "(retained)" until a
    # fresh event arrives.
    last_seen: dict[str, float] = field(default_factory=dict)
    # Device ids whose only data so far came from a retained MQTT message
    # (manager cold-start re-delivering the broker's last-known payload).
    # An id is added when a retained event/response lands without a prior
    # live update, and removed the moment a non-retained event arrives.
    retained_only: set[str] = field(default_factory=set)
    # Live online/offline state, surfaced via the bridge's `error` topic and
    # DPS events. Each entry is {"state": "online"|"offline"|"unknown",
    # "code": int|None, "message": str|None}.
    live_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Manager-emitted warnings/notes for the user, keyed by stable id (so the
    # UI deduplicates them). Each entry is {level, message, at}.
    warnings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Where the cloud devices JSON was last loaded from (None until set).
    cloud_path: str | None = None
    # Latest bridge LAN-scan sightings keyed by device id. Replaced wholesale
    # by `LanScanCoordinator` on every scan; not merged with prior results so
    # the UI always reflects the most recent broadcast (a device that didn't
    # answer this round genuinely isn't visible right now). Empty until the
    # first scan runs — distinct from "scan ran and found nothing" only by
    # the presence of an `_scan_ran_at` timestamp on the coordinator side.
    scan_results: dict[str, ScanSighting] = field(default_factory=dict)

    # Bridge-reported diagnostics from the latest `status` reply. `device_count`
    # is the bridge's authoritative total — it can exceed len(bridge) only
    # transiently while the manager pages through a large fleet; after a full
    # page-through the two agree. `mqtt_drop_count` is the bridge's cumulative
    # count of MQTT publishes it had to drop (non-zero ⇒ data loss worth
    # surfacing). `device_count` is None until the first status reply lands.
    device_count: int | None = None
    mqtt_drop_count: int = 0

    # Per-plugin state slices keyed by namespace name (see plugins.py). Empty
    # unless a plugin calls its StateNamespace.set(); `serialize_state` omits
    # the `plugins` snapshot key entirely while this is empty, so a
    # plugin-less manager stays byte-identical on the wire.
    _plugins: dict[str, Any] = field(default_factory=dict)

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

    async def set_bridge(
        self,
        devices: dict[str, Device],
        *,
        device_count: int | None = None,
        mqtt_drop_count: int | None = None,
    ) -> None:
        async with self._changed:
            self.bridge = devices
            if device_count is not None:
                self.device_count = device_count
            if mqtt_drop_count is not None:
                self.mqtt_drop_count = mqtt_drop_count
            self._bump()

    async def set_templates(self, t: BridgeTemplates) -> None:
        async with self._changed:
            self.templates = t
            self._bump()

    async def set_bridge_config_raw(self, cfg: dict[str, Any]) -> None:
        """Store the raw `{root}/bridge/config` payload dict for plugins.

        Not consumed by the manager itself (it uses `templates`); kept purely
        so `PluginContext.bridge_config()` can hand plugins the original keys.
        Doesn't bump the version — `set_templates`, called alongside it in
        `_on_bridge_config`, already wakes WS listeners for the same config
        change, so bumping here would be a redundant broadcast."""
        async with self._changed:
            self.bridge_config_raw = cfg

    async def merge_dps(
        self,
        device_id: str,
        new_dps: dict[str, Any],
        at: float | None = None,
        *,
        retained: bool = False,
    ) -> None:
        async with self._changed:
            existing = self.dps.setdefault(device_id, {})
            existing.update(new_dps)
            if retained:
                # Only mark retained_only if we haven't already seen a live
                # event — otherwise we'd downgrade a known-fresh device.
                if device_id not in self.last_seen:
                    self.retained_only.add(device_id)
            else:
                self.last_seen[device_id] = at if at is not None else _now()
                self.retained_only.discard(device_id)
            self._bump()

    async def record_response(
        self,
        target_id: str,
        response: dict[str, Any],
        at: float | None = None,
        *,
        retained: bool = False,
    ) -> None:
        async with self._changed:
            self.last_response[target_id] = response
            if retained:
                if target_id not in self.last_seen:
                    self.retained_only.add(target_id)
            else:
                self.last_seen[target_id] = at if at is not None else _now()
                self.retained_only.discard(target_id)
            self._bump()

    async def remove_device(self, device_id: str) -> None:
        """Drop a device id from every per-device bucket atomically.

        Called when the bridge confirms a `remove` action. The retained MQTT
        data for that device is cleared on the broker side too, so the
        manager must not show stale DPS / live status / last-seen entries
        after the device disappears — otherwise a device that transitions
        to "missing" (cloud-only) would still display its prior runtime
        information.
        """
        async with self._changed:
            buckets = (self.bridge, self.dps, self.live_status, self.last_seen, self.last_response)
            present = any(device_id in b for b in buckets) or device_id in self.retained_only
            if not present:
                return
            for b in buckets:
                b.pop(device_id, None)
            self.retained_only.discard(device_id)
            self._bump()

    async def clear_all_devices(self) -> None:
        """Drop every device from every per-device bucket atomically.

        Called when the bridge confirms a `clear` action: it has wiped its
        own device list and cleared the broker-side retained payloads, so
        holding on to per-device buckets would leave ghosts in the UI.
        Mirrors `remove_device` but operates on the whole fleet at once."""
        async with self._changed:
            buckets = (self.bridge, self.dps, self.live_status, self.last_seen, self.last_response)
            if not any(buckets) and not self.retained_only:
                return
            for b in buckets:
                b.clear()
            self.retained_only.clear()
            self._bump()

    async def replace_scan_results(self, sightings: dict[str, ScanSighting]) -> None:
        """Replace cached scan results with a fresh map. Wholesale replace
        (not merge) so stale entries from a previous scan don't outlive
        their scan generation — see the field's docstring."""
        async with self._changed:
            self.scan_results = dict(sightings)
            self._bump()

    async def set_plugin_data(self, name: str, data: dict[str, Any]) -> None:
        """Store a plugin's namespace data and wake WS listeners.

        Used by `plugins.StateNamespace.set`. Bumps the version unconditionally
        so the WS broadcast carries the new plugin slice; the manager core never
        inspects the contents."""
        async with self._changed:
            self._plugins[name] = data
            self._bump()

    def get_plugin_data(self, name: str) -> dict[str, Any] | None:
        return self._plugins.get(name)

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

    async def set_warning(self, key: str, level: str, message: str) -> None:
        """Surface a manager-detected issue (e.g. unparseable payload template)
        to the UI. `key` deduplicates repeats."""
        async with self._changed:
            existing = self.warnings.get(key)
            if existing and existing["level"] == level and existing["message"] == message:
                return  # no-op, don't bump version
            self.warnings[key] = {"level": level, "message": message, "at": _now()}
            self._bump()

    async def clear_warning(self, key: str) -> None:
        async with self._changed:
            if key in self.warnings:
                del self.warnings[key]
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

    async def wait_for(self, predicate, timeout: float | None = None) -> bool:
        """Awaits until `predicate()` is True (re-checked on every mutation).

        Returns True on success, False on timeout. Useful when the caller
        cares about a *semantic* condition (e.g. "bridge state is populated")
        rather than "any state change happened" — retained messages can fire
        version bumps that aren't the change you're waiting for."""
        async with self._changed:
            if predicate():
                return True
            try:
                await asyncio.wait_for(self._changed.wait_for(predicate), timeout)
                return True
            except asyncio.TimeoutError:
                return False
