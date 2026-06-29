"""BridgeClient leak regression pins.

Three surfaces:

  1. async context manager lifecycle (__aenter__/__aexit__) — the reconnect
     task must be cancelled and drained cleanly on every cycle.
  2. `subscribe_scanner` / `unsubscribe_scanner` queue pair — HIGH RISK list
     (`_scanner_subscribers`) where a caller that forgets to unsubscribe leaks
     an asyncio.Queue per scan. The list grows monotonically without
     `unsubscribe_scanner` being paired.
  3. `publish_command` cycle — the JSON-dumps + aiomqtt.publish path. Mocked
     broker keeps each cycle in microseconds; budget catches any frame or
     dict that escapes the local scope.
"""

from __future__ import annotations

import aiomqtt
import pytest

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import State

from .conftest import assert_no_leak_async, make_mqtt_fixture


async def test_bridge_client_aenter_aexit_no_leak(monkeypatch: pytest.MonkeyPatch):
    """N=30 async-with cycles must not accumulate tasks or memory.

    Patches aiomqtt.Client with a stub that raises MqttError on __aenter__ so
    the reconnect loop hits the error branch immediately — same pattern as
    tests/test_mqtt_client.py:517-544. Backoff is forced to ~0 so each cycle
    is dominated by cancel+drain, which is what we want to measure.
    """

    class _FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise aiomqtt.MqttError("test stub")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("rustuya_manager.mqtt.aiomqtt.Client", _FailingClient)
    monkeypatch.setattr(BridgeClient, "_INITIAL_BACKOFF_SEC", 0.001)
    monkeypatch.setattr(BridgeClient, "_MAX_BACKOFF_SEC", 0.001)

    async def one_cycle():
        client = BridgeClient(
            broker="mqtt://localhost:1883",
            root="rustuya",
            state=State(),
        )
        async with client:
            pass  # __aexit__ cancels the reconnect task immediately

    for _ in range(3):
        await one_cycle()
    async with assert_no_leak_async(
        max_kb=120,
        # Discrete object count, not memory (max_kb is the real-leak guard and
        # has wide headroom). py3.10's GC leaves more per-cycle residue than
        # 3.12; 30 cycles each build a fresh State (now carrying a couple more
        # default_factory lists for plugin requirements), so the count lands
        # ~650 on 3.10. Widened to absorb that — same rationale as the other
        # leak tests' py3.10 budget bumps; tracemalloc stays the real signal.
        max_objects=900,
        max_tasks=2,
        label="BridgeClient aenter/aexit",
    ):
        for _ in range(30):
            await one_cycle()


async def test_scanner_subscribers_no_leak():
    """N=200 subscribe/unsubscribe cycles must keep `_scanner_subscribers` at 0.

    The mqtt.py module comment flags this list as the place where a forgotten
    unsubscribe leaks an asyncio.Queue per scan. This test pins both the
    list-length invariant AND the tracemalloc delta — if a future refactor
    introduces an extra reference (e.g. queue retained in a closure), the
    tracemalloc budget catches it even when the list itself stays drained.
    """
    client, _ = make_mqtt_fixture()
    for _ in range(3):
        q = client.subscribe_scanner()
        client.unsubscribe_scanner(q)
    assert len(client._scanner_subscribers) == 0
    async with assert_no_leak_async(
        max_kb=60,
        max_objects=800,
        max_tasks=2,
        label="scanner subscribe/unsubscribe",
    ):
        # max_objects=800 accommodates asyncio.Queue free-list noise (each
        # Queue allocates a deque + Lock; CPython's free-list keeps some
        # alive across gc.collect calls). A real unsubscribe regression
        # would retain ALL 200 queues + their objects (>1000 objects),
        # well above this budget.
        for _ in range(200):
            q = client.subscribe_scanner()
            client.unsubscribe_scanner(q)
    assert len(client._scanner_subscribers) == 0, (
        f"_scanner_subscribers grew to {len(client._scanner_subscribers)} — "
        f"unsubscribe pair is broken"
    )


async def test_publish_command_no_leak():
    """N=200 publish_command calls must not retain per-call state.

    Each call builds a vars dict, renders a topic via pyrustuyabridge,
    json.dumps a payload, and awaits aiomqtt.publish (mocked). The whole
    transient graph should be reclaimed every cycle.
    """
    client, aio = make_mqtt_fixture()
    for _ in range(3):
        await client.publish_command("status", target_id="bridge")
    async with assert_no_leak_async(
        max_kb=60,
        max_objects=300,
        max_tasks=2,
        label="publish_command",
    ):
        for _ in range(200):
            await client.publish_command("status", target_id="bridge")
    # Sanity: the mock did see N+3 publishes (warmup + measured).
    assert aio.publish.await_count >= 200
