"""Use the installed manager's real paging handler with interleaved broadcast replies."""

import asyncio

import pytest

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import State


def page(offset, ids, total, more):
    return {
        "offset": offset,
        "returned": len(ids),
        "device_count": total,
        "has_more": more,
        "devices": {id: {"id": id, "name": id, "type": "WiFi"} for id in ids},
    }


async def test_registry_ignores_other_clients_pages_and_waits_for_complete_snapshot():
    client = BridgeClient("mqtt://localhost", "rustuya", State())
    client._bootstrap_done.set()
    client._status_active = True
    client._status_accum = {"stale": object()}
    commands = asyncio.Queue()

    async def publish(action, **kw):
        await commands.put(kw.get("extra", {}).get("offset", 0))

    client.publish_command = publish
    pending = asyncio.create_task(client.wait_registry(1))
    assert await commands.get() == 0
    try:
        await client._handle_status_page(page(2, ["c"], 3, False))
        assert not pending.done() and not client.state.bridge
        await client._handle_status_page(page(0, ["a", "b"], 3, True))
        assert await commands.get() == 2
        # Duplicate first page and a final page from a different fleet cannot commit.
        await client._handle_status_page(page(0, ["a", "b"], 3, True))
        await client._handle_status_page(page(2, ["z"], 4, False))
        await client._handle_status_page(page(2, ["a"], 3, False))
        assert not pending.done() and not client.state.bridge
        await client._handle_status_page(page(2, ["c"], 3, False))
        await pending
        assert set(client.state.bridge) == {"a", "b", "c"}
        # A later request resets pagination and rejects stale later pages too.
        await client._request_status()
        assert await commands.get() == 0
        await client._handle_status_page(page(2, ["c"], 3, False))
        assert set(client.state.bridge) == {"a", "b", "c"}
        await client._handle_status_page(page(0, [], 0, False))
        assert client.state.bridge == {}
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_registry_does_not_query_before_bootstrap_and_fails_on_timeout():
    client = BridgeClient("mqtt://localhost", "rustuya", State())
    commands = []

    async def publish(*args, **kw):
        commands.append(kw)

    client.publish_command = publish
    with pytest.raises(asyncio.TimeoutError):
        await client.wait_registry(0.01)
    assert commands == []
    client._bootstrap_done.set()
    with pytest.raises(asyncio.TimeoutError):
        await client.wait_registry(0.01)
    assert len(commands) == 1


async def test_facade_readiness_accepts_an_empty_bridge():
    from rustuya_manager import Manager

    client = BridgeClient("mqtt://localhost", "rustuya", State())
    client._bootstrap_done.set()

    async def publish(action, **kwargs):
        await client._handle_status_page(page(0, [], 0, False))

    client.publish_command = publish
    manager = Manager.__new__(Manager)
    manager.client = client
    await manager.wait_ready(bootstrap_timeout=0.1, bridge_timeout=0.1)
    assert client.state.bridge == {} and client._registry_ready.is_set()


async def test_publish_failure_does_not_report_registry_readiness():
    client = BridgeClient("mqtt://localhost", "rustuya", State())
    client._bootstrap_done.set()

    async def publish(action, **kwargs):
        raise RuntimeError("broker disconnected")

    client.publish_command = publish
    with pytest.raises(RuntimeError, match="disconnected"):
        await client.wait_registry(0.1)
    assert not client._registry_ready.is_set()


@pytest.mark.parametrize(
    "invalid",
    [
        {"offset": True},
        {"offset": -1},
        {"returned": True},
        {"returned": 2},
        {"has_more": "false"},
        {"has_more": True},
        {"device_count": True},
        {"device_count": -1},
        {"device_count": 2},
        {"devices": []},
    ],
)
async def test_malformed_page_cannot_complete_a_snapshot(invalid):
    client = BridgeClient("mqtt://localhost", "rustuya", State())
    client._status_active = True
    await client._handle_status_page({**page(0, ["a"], 1, False), **invalid})
    assert client.state.bridge == {} and client._status_offset == 0
    assert not client._registry_ready.is_set()
    await client._handle_status_page(page(0, [], 0, False))
    assert client._registry_ready.is_set()
