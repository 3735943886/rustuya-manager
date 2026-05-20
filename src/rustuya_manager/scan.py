"""Single source of truth for the bridge's LAN UDP scan.

The bridge can be told (via a `scan` MQTT command) to UDP-broadcast across
the LAN; each device that answers shows up as a sighting on the scanner
topic, terminated by an empty-dict end-marker. Two parts of the manager
want to act on that:

  - **wizard.py** — fresh cloud devices come back from Tuya with no LAN
    IP. The wizard bakes scan sightings into each record so the bridge
    can connect without a runtime UDP probe.
  - **web.py /api/scan** — the header's Scan button surfaces sightings
    directly to the UI (rendered on missing cards in PR C) so the user
    can see what's actually on the LAN, independent of cloud state.

Both call `LanScanCoordinator.run()`. The coordinator owns the
publish + subscribe + drain dance, persists sightings on
`State.scan_results` (so they ride the WS snapshot to the UI), and
serializes concurrent calls behind a single asyncio.Lock + in-flight
task so wizard + Scan button can't double-broadcast.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .state import ScanSighting

if TYPE_CHECKING:
    from .mqtt import BridgeClient
    from .state import State

logger = logging.getLogger(__name__)

# Drain budget for one scan. The bridge's UDP scanner emits sightings for
# ~18s (rustuya `DEFAULT_SCAN_TIMEOUT`), then publishes an empty dict as
# the end-marker. `_scan_once()` returns on that marker for healthy
# scans; the 2s grace covers broker-traversal latency for the marker on
# a busy broker so we don't time out a scan that already finished.
SCAN_TIMEOUT_SEC = 20.0


class LanScanCoordinator:
    """Coordinates one bridge LAN scan at a time.

    Concurrent `run()` callers (wizard launches a scan while the Scan
    button is pressed, or vice versa) share the same in-flight task and
    receive the same result — only one `scan` command goes out on the
    wire per cycle.
    """

    def __init__(self, bridge: BridgeClient, state: State) -> None:
        self._bridge = bridge
        self._state = state
        self._lock = asyncio.Lock()
        self._inflight: asyncio.Task[list[dict[str, Any]]] | None = None

    async def run(self, *, timeout: float = SCAN_TIMEOUT_SEC) -> list[dict[str, Any]]:
        """Trigger a scan, drain sightings, cache them on state, and
        return the raw sighting list in the shape
        `tuyawizard.postprocess_devices(..., scan_results=...)` expects:
        `[{id, ip?, version?, product_key?}, ...]`.

        Single-flight: if another `run()` is already in progress, this
        call awaits the same task and gets the same list back. The
        bridge sees exactly one `scan` command per logical cycle.

        Raises `RuntimeError` if publishing the scan command fails
        (broker disconnected, etc.) — caller decides whether to degrade
        (wizard) or surface a toast (web endpoint).
        """
        # Fast path: an in-flight scan exists, just await it. We don't
        # take the lock here because waiting on the existing task is
        # itself contention-free — the lock is only there to make
        # "decide whether to start a new task" atomic.
        if self._inflight is not None and not self._inflight.done():
            return await self._inflight

        async with self._lock:
            # Re-check under the lock — a concurrent caller may have
            # started a task between our fast-path check and acquiring
            # the lock.
            if self._inflight is not None and not self._inflight.done():
                return await self._inflight
            task = asyncio.create_task(self._scan_once(timeout))
            self._inflight = task
        try:
            return await task
        finally:
            # Only clear if we're still the current task — protects
            # against a fresh scan starting between `task` finishing and
            # us reaching this `finally`.
            if self._inflight is task:
                self._inflight = None

    async def _scan_once(self, timeout: float) -> list[dict[str, Any]]:
        """Subscribe to scanner sightings, publish the scan command,
        drain until either the end-marker or the timeout, and store
        the result on state."""
        q = self._bridge.subscribe_scanner()
        results: list[dict[str, Any]] = []
        try:
            await self._bridge.publish_command("scan", target_id="bridge")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=remaining)
                except TimeoutError:
                    break
                if not item:
                    # Empty dict is the bridge's explicit scan-end
                    # marker; return immediately, ignoring any remaining
                    # timeout budget.
                    break
                results.append(item)
        finally:
            self._bridge.unsubscribe_scanner(q)

        now = time.time()
        sightings = {
            r["id"]: ScanSighting(
                id=r["id"],
                ip=r.get("ip"),
                version=r.get("version"),
                observed_at=now,
            )
            for r in results
            if isinstance(r, dict) and r.get("id")
        }
        await self._state.replace_scan_results(sightings)
        return results
