"""LanScanCoordinator leak regression pin.

The coordinator's `run()` subscribes a queue, publishes a scan command,
drains until end-marker or timeout, then unsubscribes in a `finally`.
A regression that drops the finally would leak both the queue AND its
reference through `_scanner_subscribers` on every scan.

We exercise the timeout branch (no end-marker arrives) so cycles are fast
and the queue.get path is the dominant code under measurement.
"""

from __future__ import annotations

from rustuya_manager.scan import LanScanCoordinator

from .conftest import assert_no_leak_async, make_mqtt_fixture


async def test_scan_run_timeout_no_leak():
    """N=20 scan timeouts must not accumulate queues or memory.

    Each cycle: subscribe queue → publish_command (mocked) → wait_for queue.get
    with 50 ms deadline → TimeoutError → unsubscribe (in finally) → return [].
    Since no scanner items arrive, no `_dispatch` ever puts to the queue —
    we measure pure subscribe/unsubscribe + queue/get path overhead.
    """
    client, _ = make_mqtt_fixture()
    state = client.state
    coord = LanScanCoordinator(client, state)
    for _ in range(3):
        await coord.run(timeout=0.05)
    async with assert_no_leak_async(
        max_kb=80,
        max_objects=400,
        max_tasks=2,
        label="LanScanCoordinator run timeout",
    ):
        for _ in range(20):
            await coord.run(timeout=0.05)
    assert len(client._scanner_subscribers) == 0, (
        f"scanner subscribers leaked through coord.run: "
        f"{len(client._scanner_subscribers)}"
    )
