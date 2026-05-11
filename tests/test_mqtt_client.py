"""Unit tests for the BridgeClient callbacks and dispatch routing.

These don't need a real broker. paho is mocked so we can drive the callbacks
synchronously and inspect what subscribe()/publish() were called with.

Coverage focus (matches the user's MQTT-top-priority concern):
  - on_connect re-subscribes runtime wildcards on reconnect
  - on_connect refuses to subscribe on a failed CONNACK
  - dispatch correctly routes incoming topics with custom templates
  - publish_command renders both topic and payload from kwargs
  - empty payloads (retain-clearing) are skipped
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rustuya_manager.mqtt import BRIDGE_CONFIG_TOPIC_TPL, BridgeClient
from rustuya_manager.state import BridgeTemplates, State

import paho.mqtt.client as mqtt


# Sample bridge/config that mirrors the custom topology used in our e2e:
# - root has a slash (myhome/tuya)
# - message_topic uses an unrelated prefix (tuyalog/)
# - command_topic and event_topic both carry variables
CUSTOM_CONFIG = {
    "mqtt_root_topic": "myhome/tuya",
    "mqtt_command_topic": "{root}/cmd/{id}/{action}",
    "mqtt_event_topic": "{root}/dev/{name}/dp/{dp}/state",
    "mqtt_message_topic": "tuyalog/{level}/{id}",
    "mqtt_scanner_topic": "{root}/scanner",
    "mqtt_payload_template": "{value}",
}


def _make_client(state: State | None = None) -> tuple[BridgeClient, MagicMock]:
    """Builds a BridgeClient with paho mocked out. The mock replaces
    `_make_client`, so connect/subscribe/publish go to the mock and we can
    assert on them."""
    state = state or State()
    client = BridgeClient(
        broker="mqtt://localhost:1883",
        root="myhome/tuya",
        state=state,
    )
    mock_paho = MagicMock(spec=mqtt.Client)
    mock_paho.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
    mock_paho.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    client._client = mock_paho
    client._loop = asyncio.get_event_loop()
    return client, mock_paho


# ─────────────────────────────────────────────────────────────────────────────
# on_connect / re-subscribe
# ─────────────────────────────────────────────────────────────────────────────

class TestOnConnect:
    def test_first_connect_subscribes_bridge_config_only(self):
        client, paho = _make_client()
        # paho v2 signature
        client._on_connect(paho, None, {}, SimpleNamespace(is_failure=False, value=0), None)
        # Only bridge/config — runtime wildcards aren't known yet
        subscribed_topics = [call.args[0] for call in paho.subscribe.call_args_list]
        assert subscribed_topics == ["myhome/tuya/bridge/config"]

    def test_reconnect_replays_runtime_subscriptions(self):
        """After bootstrap, a reconnect must re-subscribe to event/message/scanner —
        otherwise a broker hiccup silently kills the event stream."""
        client, paho = _make_client()
        client._runtime_wildcards = [
            "myhome/tuya/dev/+/dp/+/state",
            "tuyalog/+/+",
            "myhome/tuya/scanner",
        ]
        client._on_connect(paho, None, {}, SimpleNamespace(is_failure=False, value=0), None)
        subscribed_topics = [call.args[0] for call in paho.subscribe.call_args_list]
        # bridge/config + the three runtime wildcards, in that order
        assert subscribed_topics == [
            "myhome/tuya/bridge/config",
            "myhome/tuya/dev/+/dp/+/state",
            "tuyalog/+/+",
            "myhome/tuya/scanner",
        ]

    def test_failed_connack_does_not_subscribe(self):
        client, paho = _make_client()
        client._runtime_wildcards = ["myhome/tuya/dev/+/dp/+/state"]
        client._on_connect(paho, None, {}, SimpleNamespace(is_failure=True, value=5), None)
        assert paho.subscribe.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# dispatch routing (custom templates)
# ─────────────────────────────────────────────────────────────────────────────

class TestDispatch:
    @pytest.mark.asyncio
    async def test_bridge_config_redelivery_does_not_resubscribe(self):
        """Regression: when the bridge uses the default message topic
        `{root}/{level}/{id}` the wildcard `{root}/+/+` also matches the
        retained `{root}/bridge/config` topic, so the broker re-delivers it
        every time we subscribe. Without idempotence this caused an infinite
        bootstrap loop on a real test server.

        After the first bootstrap, a second dispatch of the same config must
        NOT issue any more subscribe calls."""
        state = State()
        client, paho = _make_client(state)
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", "myhome/tuya")
        cfg_payload = json.dumps({
            "mqtt_root_topic": "myhome/tuya",
            "mqtt_command_topic": "{root}/command",
            "mqtt_event_topic": "{root}/event/{type}/{id}",
            "mqtt_message_topic": "{root}/{level}/{id}",
            "mqtt_scanner_topic": "{root}/scanner",
            "mqtt_payload_template": "{value}",
        })
        await client._dispatch(cfg_topic, cfg_payload)
        first_subscribe_count = paho.subscribe.call_count
        first_publish_count = paho.publish.call_count
        assert first_subscribe_count >= 3, "expected runtime wildcards subscribed once"

        # The broker re-delivers the SAME retained config. Manager must be a
        # no-op — no additional subscribes, no additional status request.
        await client._dispatch(cfg_topic, cfg_payload)
        assert paho.subscribe.call_count == first_subscribe_count
        assert paho.publish.call_count == first_publish_count

    @pytest.mark.asyncio
    async def test_bridge_config_resolves_templates(self):
        state = State()
        client, _ = _make_client(state)
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", "myhome/tuya")

        await client._dispatch(cfg_topic, json.dumps(CUSTOM_CONFIG))

        assert state.templates is not None
        assert state.templates.event == "myhome/tuya/dev/{name}/dp/{dp}/state"
        assert state.templates.message == "tuyalog/{level}/{id}"
        assert state.templates.command == "myhome/tuya/cmd/{id}/{action}"
        # The reconnect-replay cache must be populated as a side effect.
        assert set(client._runtime_wildcards) == {
            "myhome/tuya/dev/+/dp/+/state",
            "tuyalog/+/+",
            "myhome/tuya/scanner",
        }
        assert client._bootstrap_done.is_set()

    @pytest.mark.asyncio
    async def test_event_topic_updates_dps(self):
        state = State()
        await state.set_templates(BridgeTemplates(
            root="myhome/tuya",
            command="myhome/tuya/cmd/{id}/{action}",
            event="myhome/tuya/dev/{name}/dp/{dp}/state",
            message="tuyalog/{level}/{id}",
            scanner="myhome/tuya/scanner",
            payload="{value}",
        ))
        client, _ = _make_client(state)
        await client._dispatch("myhome/tuya/dev/kitchen/dp/1/state", "true")
        assert "kitchen" in state.dps
        assert state.dps["kitchen"] == {"1": True}

    @pytest.mark.asyncio
    async def test_message_topic_status_response_sets_bridge_devices(self):
        state = State()
        await state.set_templates(BridgeTemplates(
            root="myhome/tuya",
            command="myhome/tuya/cmd/{id}/{action}",
            event="myhome/tuya/dev/{name}/dp/{dp}/state",
            message="tuyalog/{level}/{id}",
            scanner="myhome/tuya/scanner",
            payload="{value}",
        ))
        client, _ = _make_client(state)
        await client._dispatch(
            "tuyalog/response/bridge",
            json.dumps({
                "action": "status",
                "devices": {
                    "bf-aaaa": {"id": "bf-aaaa", "ip": "192.168.1.10", "key": "k1", "status": "online"},
                },
                "id": "bridge",
                "status": "ok",
            }),
        )
        assert "bf-aaaa" in state.bridge
        assert state.bridge["bf-aaaa"].ip == "192.168.1.10"
        # last_response also captured
        assert "bridge" in state.last_response

    @pytest.mark.asyncio
    async def test_empty_payload_skipped(self):
        """Retain-clearing publishes empty payload — must not crash or pollute state."""
        state = State()
        await state.set_templates(BridgeTemplates(
            root="myhome/tuya",
            command="myhome/tuya/cmd/{id}/{action}",
            event="myhome/tuya/dev/{name}/dp/{dp}/state",
            message="tuyalog/{level}/{id}",
            scanner="myhome/tuya/scanner",
            payload="{value}",
        ))
        client, _ = _make_client(state)
        v_before = state.version
        await client._dispatch("myhome/tuya/dev/kitchen/dp/1/state", "")
        # No change to dps and no exception
        assert state.dps == {}
        assert state.version == v_before

    @pytest.mark.asyncio
    async def test_bridge_config_clear_is_ignored(self):
        """Bridge publishes empty retained on graceful shutdown — manager must
        not interpret that as a fresh config."""
        state = State()
        client, _ = _make_client(state)
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", "myhome/tuya")
        await client._dispatch(cfg_topic, "")
        assert state.templates is None


# ─────────────────────────────────────────────────────────────────────────────
# publish_command
# ─────────────────────────────────────────────────────────────────────────────

class TestPublishCommand:
    @pytest.mark.asyncio
    async def test_renders_topic_with_id_and_action(self):
        state = State()
        await state.set_templates(BridgeTemplates(
            root="myhome/tuya",
            command="myhome/tuya/cmd/{id}/{action}",
            event="myhome/tuya/dev/{name}/dp/{dp}/state",
            message="tuyalog/{level}/{id}",
            scanner="myhome/tuya/scanner",
            payload="{value}",
        ))
        client, paho = _make_client(state)
        await client.publish_command("status", target_id="bridge")
        paho.publish.assert_called_once()
        topic, body = paho.publish.call_args.args[:2]
        assert topic == "myhome/tuya/cmd/bridge/status"
        parsed_body = json.loads(body)
        assert parsed_body == {"action": "status", "id": "bridge"}

    @pytest.mark.asyncio
    async def test_command_without_vars_in_topic(self):
        """Default command_topic is `{root}/command` with no vars — vars go in payload."""
        state = State()
        await state.set_templates(BridgeTemplates(
            root="rustuya",
            command="rustuya/command",
            event="rustuya/event/{type}/{id}",
            message="rustuya/{level}/{id}",
            scanner="rustuya/scanner",
            payload="{value}",
        ))
        client, paho = _make_client(state)
        await client.publish_command("add", target_id="bf123", extra={"key": "k", "ip": "1.2.3.4"})
        topic, body = paho.publish.call_args.args[:2]
        assert topic == "rustuya/command"
        assert json.loads(body) == {
            "action": "add",
            "id": "bf123",
            "key": "k",
            "ip": "1.2.3.4",
        }

    @pytest.mark.asyncio
    async def test_publish_before_bootstrap_raises(self):
        client, _ = _make_client()
        with pytest.raises(RuntimeError, match="not yet resolved"):
            await client.publish_command("status")


# pytest-asyncio integration — auto mode is the simplest setup for our needs.
def pytest_collection_modifyitems(items):
    for item in items:
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)
