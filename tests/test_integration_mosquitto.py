"""Live-broker integration tests for the aiomqtt-backed BridgeClient.

Where `test_mqtt_client.py` mocks aiomqtt and `test_e2e_bridge.py` runs the
full bridge in-process, this module sits in the middle: a real mosquitto
broker on localhost, but the "bridge" side is faked with a second aiomqtt
client. That isolates the manager's MQTT layer from rustuya-bridge
specifics so any regression here points squarely at our async client.

The whole module skips if mosquitto is not reachable on localhost:1883.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time

import aiomqtt
import pytest

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import State


def _broker_reachable() -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", 1883))
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _broker_reachable(), reason="no MQTT broker on localhost:1883")

# Unique root per test run so retained state from a prior run can't leak in.
ROOT = f"rustuya_mgr_integration_{os.getpid()}_{int(time.time())}"


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _publish_retained_config(root: str, payload: dict) -> None:
    """Publish bridge/config as the bridge would: retained, QoS 1."""
    async with aiomqtt.Client(hostname="localhost", port=1883) as c:
        await c.publish(f"{root}/bridge/config", json.dumps(payload), qos=1, retain=True)


async def _clear_retained_config(root: str) -> None:
    """Wipe the retained bridge/config so a re-run starts clean."""
    async with aiomqtt.Client(hostname="localhost", port=1883) as c:
        await c.publish(f"{root}/bridge/config", b"", qos=1, retain=True)


@pytest.fixture
async def unique_root():
    """Per-test root with retained-config cleanup on teardown."""
    root = f"{ROOT}_{int(time.time() * 1000)}"
    try:
        yield root
    finally:
        await _clear_retained_config(root)


# ─────────────────────────────────────────────────────────────────────────────
# tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_context_manager_bootstraps_against_real_broker(unique_root):
    """The minimum smoke test: `async with BridgeClient` reaches the broker,
    consumes the retained bridge/config we publish, and resolves templates."""
    root = unique_root
    await _publish_retained_config(
        root,
        {
            "mqtt_root_topic": root,
            "mqtt_command_topic": "{root}/command",
            "mqtt_event_topic": "{root}/event/{type}/{id}",
            "mqtt_message_topic": "{root}/{level}/{id}",
            "mqtt_scanner_topic": "{root}/scanner",
            "mqtt_payload_template": "{value}",
        },
    )

    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=root, state=state)
    async with client:
        await client.wait_bootstrap(timeout=5.0)
        assert client._bootstrap_done.is_set(), "bootstrap should have completed"
        assert state.templates is not None
        assert state.templates.root == root
        assert state.templates.event == f"{root}/event/{{type}}/{{id}}"
        # broker_unreachable warning must NOT be set on a clean connect
        assert "broker_unreachable" not in state.warnings
        assert "bridge_offline" not in state.warnings


async def test_publish_command_round_trips_through_broker(unique_root):
    """publish_command must produce an actual MQTT message a second client
    can observe. Verifies the aiomqtt `await client.publish(...)` path end
    to end."""
    root = unique_root
    await _publish_retained_config(
        root,
        {
            "mqtt_root_topic": root,
            "mqtt_command_topic": "{root}/cmd/{id}/{action}",
            "mqtt_event_topic": "{root}/event/{type}/{id}",
            "mqtt_message_topic": "{root}/{level}/{id}",
            "mqtt_scanner_topic": "{root}/scanner",
            "mqtt_payload_template": "{value}",
        },
    )

    # Subscriber-side: a second aiomqtt client listens for the command topic
    # the manager is about to publish to. Use a future so the assertion can
    # wait with a bounded timeout.
    received: asyncio.Future[tuple[str, str]] = asyncio.get_event_loop().create_future()

    async def subscriber():
        async with aiomqtt.Client(hostname="localhost", port=1883) as sub:
            await sub.subscribe(f"{root}/cmd/+/+")
            async for msg in sub.messages:
                if not received.done():
                    received.set_result((str(msg.topic), msg.payload.decode()))
                return

    sub_task = asyncio.create_task(subscriber())
    # Give the subscriber a beat to actually be subscribed before we publish.
    await asyncio.sleep(0.2)

    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=root, state=state)
    async with client:
        await client.wait_bootstrap(timeout=5.0)
        # Bootstrap itself also publishes a status command — the subscriber
        # may catch either that one or the explicit one below; both are
        # valid wins for "the publish path reaches the broker".
        await client.publish_command("status", target_id="bridge")

        topic, body = await asyncio.wait_for(received, timeout=3.0)
        assert topic.startswith(f"{root}/cmd/")
        parsed = json.loads(body)
        assert parsed["action"] == "status"

    sub_task.cancel()
    try:
        await sub_task
    except (asyncio.CancelledError, aiomqtt.MqttError):
        pass


async def test_status_response_populates_state_bridge(unique_root):
    """A bridge `status` reply on the message topic must end up in
    state.bridge — exercises the full receive → dispatch → state path
    across a real broker."""
    root = unique_root
    await _publish_retained_config(
        root,
        {
            "mqtt_root_topic": root,
            "mqtt_command_topic": "{root}/command",
            "mqtt_event_topic": "{root}/event/{type}/{id}",
            "mqtt_message_topic": "{root}/{level}/{id}",
            "mqtt_scanner_topic": "{root}/scanner",
            "mqtt_payload_template": "{value}",
        },
    )

    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=root, state=state)
    async with client:
        await client.wait_bootstrap(timeout=5.0)

        # Publish a fake status response on the response topic the manager
        # is subscribed to (response level, id=bridge).
        async with aiomqtt.Client(hostname="localhost", port=1883) as publisher:
            await publisher.publish(
                f"{root}/response/bridge",
                json.dumps(
                    {
                        "action": "status",
                        "id": "bridge",
                        "status": "ok",
                        "devices": {
                            "bf-int-1": {
                                "id": "bf-int-1",
                                "ip": "10.99.0.1",
                                "key": "k_int",
                                "status": "online",
                            }
                        },
                    }
                ),
                qos=1,
            )

        # Wait for the dispatch to land in state.
        for _ in range(50):
            if "bf-int-1" in state.bridge:
                break
            await asyncio.sleep(0.05)
        assert "bf-int-1" in state.bridge, (
            f"status response not consumed (devices={list(state.bridge.keys())})"
        )
        assert state.bridge["bf-int-1"].ip == "10.99.0.1"


async def test_wait_bootstrap_timeout_applies_default_templates(unique_root):
    """When no bridge/config arrives, the internal timeout guard must
    eventually apply default templates and mark the bridge as offline."""
    root = unique_root
    # Deliberately do NOT publish a bridge/config — bridge is offline.

    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=root, state=state)
    # Shorten the internal guard so the test doesn't sit for 5s.
    import rustuya_manager.mqtt as mqtt_mod

    original = mqtt_mod.BOOTSTRAP_TIMEOUT_SEC
    mqtt_mod.BOOTSTRAP_TIMEOUT_SEC = 0.3
    try:
        async with client:
            # Wait for the fallback path to fire — wait_bootstrap returns
            # silently on timeout, but the guard task in _reconnect_loop
            # applies defaults and sets bootstrap_done shortly after.
            await client.wait_bootstrap(timeout=2.0)
            assert client._bootstrap_done.is_set()
            assert state.templates is not None
            assert state.templates.root == root
            # The hallmark "bridge offline, using defaults" warning must be set.
            assert "bridge_offline" in state.warnings
    finally:
        mqtt_mod.BOOTSTRAP_TIMEOUT_SEC = original


async def test_publish_before_bootstrap_raises_runtime_error(unique_root):
    """Before templates resolve, publish_command must raise RuntimeError so
    FastAPI handlers translate it to a clear 503 rather than hanging."""
    root = unique_root
    # No bridge/config published — templates never resolve in the test window.

    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=root, state=state)
    async with client:
        # Wait briefly for the connection to come up (so _connected is set)
        # but NOT long enough for the bootstrap guard to apply fallbacks.
        for _ in range(20):
            if client._connected.is_set():
                break
            await asyncio.sleep(0.05)
        assert client._connected.is_set(), "broker connect should have succeeded"

        # _connected is set, but templates is None → publish_command should
        # surface the "templates not yet resolved" error specifically.
        with pytest.raises(RuntimeError, match="not yet resolved"):
            await client.publish_command("status", target_id="bridge")


async def test_clean_exit_releases_broker_connection(unique_root):
    """Two sequential `async with` blocks on the same client_id must both
    succeed — verifies __aexit__ actually closes the aiomqtt session rather
    than leaving a half-open one that would collide on reconnect."""
    root = unique_root
    await _publish_retained_config(
        root,
        {
            "mqtt_root_topic": root,
            "mqtt_command_topic": "{root}/command",
            "mqtt_event_topic": "{root}/event/{type}/{id}",
            "mqtt_message_topic": "{root}/{level}/{id}",
            "mqtt_scanner_topic": "{root}/scanner",
            "mqtt_payload_template": "{value}",
        },
    )

    state = State()
    for _ in range(2):
        client = BridgeClient(
            broker="mqtt://localhost:1883",
            root=root,
            state=state,
            client_id="rustuya-manager-cleanup-test",
        )
        async with client:
            await client.wait_bootstrap(timeout=5.0)
            assert client._bootstrap_done.is_set()
