"""Tests for LanScanCoordinator.

Why these matter: the coordinator is the *single* place that turns a
manager-side intent ("do a LAN scan") into bridge MQTT traffic. Both the
wizard scan toggle and the header Scan button funnel through here, so the
single-flight guard, the drain logic, and the end-marker handling are
load-bearing for the project's DRY constraint — if either caller could
shortcut the coordinator, we'd be back to the duplicated wiring this PR
is removing.

We exercise the coordinator against a fake BridgeClient so the unit tests
don't touch a real broker. The fake exposes the same surface the
coordinator uses: `subscribe_scanner()`/`unsubscribe_scanner()` queue
management and `publish_command()` (just records calls).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rustuya_manager.scan import LanScanCoordinator
from rustuya_manager.state import State

pytestmark = pytest.mark.asyncio


class _FakeBridge:
    """Minimal stand-in for BridgeClient — enough surface for the
    coordinator without dragging in aiomqtt. `feed()` is a test-side
    helper that pushes a sighting onto every active subscriber queue,
    mirroring what mqtt.py does when a real scanner-topic message
    arrives."""

    def __init__(self, *, publish_error: Exception | None = None) -> None:
        self._subs: list[asyncio.Queue[dict[str, Any]]] = []
        self.published: list[tuple[str, str | None]] = []
        self._publish_error = publish_error

    def subscribe_scanner(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe_scanner(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    async def publish_command(
        self,
        action: str,
        target_id: str | None = None,
        target_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((action, target_id))
        if self._publish_error is not None:
            raise self._publish_error

    def feed(self, sighting: dict[str, Any]) -> None:
        """Test-side hook: deliver one payload to every subscriber, the
        same way mqtt.py's scanner-topic dispatcher does. Empty dict
        terminates the scan."""
        for q in list(self._subs):
            q.put_nowait(sighting)


async def test_run_returns_raw_sightings_and_terminates_on_empty_marker():
    """Healthy scan: sightings stream in, end-marker (empty dict) cuts
    the drain short, raw list returns in the order received."""
    bridge = _FakeBridge()
    state = State()
    coord = LanScanCoordinator(bridge, state)

    # Push sightings + end-marker as soon as `run()` has subscribed. We
    # can't synchronously know when subscribe happens, so use a tiny
    # delay before feeding — the coordinator awaits queue.get(), so it's
    # already parked by the time this task runs.
    async def feeder():
        await asyncio.sleep(0)  # yield once so coord can subscribe
        bridge.feed({"id": "dev-a", "ip": "10.0.0.5", "version": "3.4"})
        bridge.feed({"id": "dev-b", "ip": "10.0.0.7"})
        bridge.feed({})  # end-marker

    asyncio.create_task(feeder())
    results = await coord.run(timeout=2.0)

    assert results == [
        {"id": "dev-a", "ip": "10.0.0.5", "version": "3.4"},
        {"id": "dev-b", "ip": "10.0.0.7"},
    ]
    assert bridge.published == [("scan", "bridge")]
    # State got the converted ScanSighting dict
    assert set(state.scan_results) == {"dev-a", "dev-b"}
    a = state.scan_results["dev-a"]
    assert a.ip == "10.0.0.5"
    assert a.version == "3.4"
    assert a.observed_at > 0


async def test_run_falls_through_on_timeout_without_end_marker():
    """If the bridge never publishes the empty end-marker (broker
    hiccup, dropped retain), drain stops cleanly at `timeout` with
    whatever sightings arrived — no exception, no hang."""
    bridge = _FakeBridge()
    coord = LanScanCoordinator(bridge, State())

    async def feeder():
        await asyncio.sleep(0)
        bridge.feed({"id": "dev-a", "ip": "10.0.0.5"})
        # no end-marker; the timeout has to cut us off

    asyncio.create_task(feeder())
    results = await coord.run(timeout=0.2)
    assert results == [{"id": "dev-a", "ip": "10.0.0.5"}]


async def test_run_publish_failure_propagates_for_caller_to_classify():
    """publish_command surfaces broker-disconnect as RuntimeError; the
    coordinator must let it through unchanged so wizard.py can degrade
    to parent-only and web.py can return 503."""
    bridge = _FakeBridge(publish_error=RuntimeError("MQTT not connected"))
    state = State()
    coord = LanScanCoordinator(bridge, state)

    with pytest.raises(RuntimeError, match="not connected"):
        await coord.run(timeout=0.1)
    # state.scan_results stays untouched on publish failure — we never
    # got past `publish_command`, so there's nothing to cache.
    assert state.scan_results == {}


async def test_concurrent_run_calls_share_one_in_flight_scan():
    """Single-flight: two `run()` calls that overlap must result in one
    `publish_command` on the wire and both callers get the same list.
    This is the property the PR exists to enforce — wizard scan + Scan
    button must not double-broadcast."""
    bridge = _FakeBridge()
    coord = LanScanCoordinator(bridge, State())

    async def feeder():
        # Wait long enough that both run() calls have parked on q.get
        # before we close the scan out.
        await asyncio.sleep(0.05)
        bridge.feed({"id": "dev-a", "ip": "10.0.0.5"})
        bridge.feed({})

    asyncio.create_task(feeder())
    r1, r2 = await asyncio.gather(coord.run(timeout=2.0), coord.run(timeout=2.0))

    assert r1 == r2 == [{"id": "dev-a", "ip": "10.0.0.5"}]
    # Exactly one scan command on the wire
    assert bridge.published == [("scan", "bridge")]


async def test_back_to_back_runs_each_publish_and_replace_state():
    """Sequential `run()` calls (not overlapping) must each issue their
    own `scan` command and the second run's sightings must wholesale
    replace the first's — stale entries don't survive across cycles."""
    bridge = _FakeBridge()
    state = State()
    coord = LanScanCoordinator(bridge, state)

    async def feed_then(payload: dict[str, Any]):
        await asyncio.sleep(0)
        bridge.feed(payload)
        bridge.feed({})

    asyncio.create_task(feed_then({"id": "old", "ip": "10.0.0.1"}))
    await coord.run(timeout=2.0)
    assert set(state.scan_results) == {"old"}

    asyncio.create_task(feed_then({"id": "new", "ip": "10.0.0.2"}))
    await coord.run(timeout=2.0)
    # Wholesale replace — "old" must not linger
    assert set(state.scan_results) == {"new"}
    assert bridge.published == [("scan", "bridge"), ("scan", "bridge")]


async def test_run_drops_malformed_sightings_without_id():
    """The bridge contract is "sighting dicts carry `id`"; anything
    without one is treated as noise so the state map stays a clean
    `id -> sighting` lookup the UI can rely on."""
    bridge = _FakeBridge()
    state = State()
    coord = LanScanCoordinator(bridge, state)

    async def feeder():
        await asyncio.sleep(0)
        bridge.feed({"ip": "10.0.0.5"})  # no id — dropped from state
        bridge.feed({"id": "ok", "ip": "10.0.0.6"})
        bridge.feed({})

    asyncio.create_task(feeder())
    results = await coord.run(timeout=2.0)
    # Raw list passes through unchanged — postprocess_devices gets to
    # decide what to do with malformed entries.
    assert len(results) == 2
    # State map only keeps the well-formed one.
    assert set(state.scan_results) == {"ok"}
