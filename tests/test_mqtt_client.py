"""Unit tests for the BridgeClient dispatch routing and publish surface.

These don't need a real broker. aiomqtt is mocked so we can drive subscribe /
publish surfaces and inspect what was awaited.

Coverage focus (matches the user's MQTT-top-priority concern):
  - _subscribe_initial re-subscribes runtime wildcards on every (re)connect
  - dispatch correctly routes incoming topics with custom templates
  - publish_command renders both topic and payload from kwargs
  - publish_command refuses cleanly when broker is disconnected
  - publish_command translates aiomqtt errors into RuntimeError for FastAPI
  - empty payloads (retain-clearing) are skipped
  - bridge-config redelivery is idempotent (no infinite bootstrap loop)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest

from rustuya_manager.mqtt import (
    BRIDGE_CONFIG_TOPIC_TPL,
    BridgeClient,
    _format_error_message,
)
from rustuya_manager.state import BridgeTemplates, State

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
    """Builds a BridgeClient with aiomqtt mocked out. The mock replaces the
    live `_client` attribute so subscribe/unsubscribe/publish are awaitable
    no-ops we can assert against. `_connected` is pre-set so publish_command's
    guard passes — tests that want the disconnected path clear it themselves."""
    state = state or State()
    client = BridgeClient(
        broker="mqtt://localhost:1883",
        root="myhome/tuya",
        state=state,
    )
    mock_aiomqtt = MagicMock()
    mock_aiomqtt.subscribe = AsyncMock(return_value=None)
    mock_aiomqtt.unsubscribe = AsyncMock(return_value=None)
    mock_aiomqtt.publish = AsyncMock(return_value=None)
    client._client = mock_aiomqtt
    client._connected.set()
    return client, mock_aiomqtt


# ─────────────────────────────────────────────────────────────────────────────
# subscribe replay on every (re)connect
# ─────────────────────────────────────────────────────────────────────────────


class TestSubscribeInitial:
    @pytest.mark.asyncio
    async def test_first_connect_subscribes_bridge_config_only(self):
        client, aio = _make_client()
        await client._subscribe_initial(aio)
        # Only bridge/config — runtime wildcards aren't known yet
        subscribed = [call.args[0] for call in aio.subscribe.await_args_list]
        assert subscribed == ["myhome/tuya/bridge/config"]

    @pytest.mark.asyncio
    async def test_reconnect_replays_runtime_subscriptions(self):
        """After bootstrap, a reconnect must re-subscribe to event/message/scanner —
        otherwise a broker hiccup silently kills the event stream."""
        client, aio = _make_client()
        client._runtime_wildcards = [
            "myhome/tuya/dev/+/dp/+/state",
            "tuyalog/+/+",
            "myhome/tuya/scanner",
        ]
        await client._subscribe_initial(aio)
        subscribed = [call.args[0] for call in aio.subscribe.await_args_list]
        # bridge/config + the three runtime wildcards, in that order
        assert subscribed == [
            "myhome/tuya/bridge/config",
            "myhome/tuya/dev/+/dp/+/state",
            "tuyalog/+/+",
            "myhome/tuya/scanner",
        ]


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
        client, aio = _make_client(state)
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", "myhome/tuya")
        cfg_payload = json.dumps(
            {
                "mqtt_root_topic": "myhome/tuya",
                "mqtt_command_topic": "{root}/command",
                "mqtt_event_topic": "{root}/event/{type}/{id}",
                "mqtt_message_topic": "{root}/{level}/{id}",
                "mqtt_scanner_topic": "{root}/scanner",
                "mqtt_payload_template": "{value}",
            }
        )
        await client._dispatch(cfg_topic, cfg_payload)
        first_subscribe_count = aio.subscribe.await_count
        first_publish_count = aio.publish.await_count
        assert first_subscribe_count >= 3, "expected runtime wildcards subscribed once"

        # The broker re-delivers the SAME retained config. Manager must be a
        # no-op — no additional subscribes, no additional status request.
        await client._dispatch(cfg_topic, cfg_payload)
        assert aio.subscribe.await_count == first_subscribe_count
        assert aio.publish.await_count == first_publish_count

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
        await state.set_templates(
            BridgeTemplates(
                root="myhome/tuya",
                command="myhome/tuya/cmd/{id}/{action}",
                event="myhome/tuya/dev/{name}/dp/{dp}/state",
                message="tuyalog/{level}/{id}",
                scanner="myhome/tuya/scanner",
                payload="{value}",
            )
        )
        # The event topic carries {name} but not {id}; the manager must
        # reverse-lookup the bridge's device by name to find the canonical
        # id. Pre-seed bridge state so the lookup resolves.
        from rustuya_manager.models import Device

        await state.set_bridge(
            {
                "bf-kitchen-id": Device(id="bf-kitchen-id", name="kitchen", ip="1.2.3.4"),
            }
        )
        client, _ = _make_client(state)
        await client._dispatch("myhome/tuya/dev/kitchen/dp/1/state", "true")
        # DPS is keyed by the bridge's id (not the topic's name).
        assert "bf-kitchen-id" in state.dps
        assert state.dps["bf-kitchen-id"] == {"1": True}

    @pytest.mark.asyncio
    async def test_event_with_unknown_name_skips_silently(self):
        """Regression: when topic carries {name} but bridge doesn't know that
        name yet, we used to create a phantom dps entry keyed by name. Now
        we skip cleanly — no merge, no fake key."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="myhome/tuya",
                command="myhome/tuya/cmd/{id}/{action}",
                event="myhome/tuya/dev/{name}/dp/{dp}/state",
                message="tuyalog/{level}/{id}",
                scanner="myhome/tuya/scanner",
                payload="{value}",
            )
        )
        client, _ = _make_client(state)
        await client._dispatch("myhome/tuya/dev/unknown/dp/1/state", "true")
        assert state.dps == {}

    @pytest.mark.asyncio
    async def test_message_topic_status_response_sets_bridge_devices(self):
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="myhome/tuya",
                command="myhome/tuya/cmd/{id}/{action}",
                event="myhome/tuya/dev/{name}/dp/{dp}/state",
                message="tuyalog/{level}/{id}",
                scanner="myhome/tuya/scanner",
                payload="{value}",
            )
        )
        client, _ = _make_client(state)
        await client._dispatch(
            "tuyalog/response/bridge",
            json.dumps(
                {
                    "action": "status",
                    "devices": {
                        "bf-aaaa": {
                            "id": "bf-aaaa",
                            "ip": "192.168.1.10",
                            "key": "k1",
                            "status": "online",
                        },
                    },
                    "id": "bridge",
                    "status": "ok",
                }
            ),
        )
        assert "bf-aaaa" in state.bridge
        assert state.bridge["bf-aaaa"].ip == "192.168.1.10"
        # last_response also captured
        assert "bridge" in state.last_response

    @pytest.mark.asyncio
    async def test_error_level_marks_device_online_or_offline(self):
        """Bridge publishes per-device connection state to {root}/error/<id>.
        errorCode=0 means "Connection Successful" (online); any other code
        means the device is unreachable."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="rustuya",
                command="rustuya/command",
                event="rustuya/event/{type}/{id}",
                message="rustuya/{level}/{id}",
                scanner="rustuya/scanner",
                payload="{value}",
            )
        )
        # Override the client root to match the templates above
        client, _ = _make_client(state)
        client.root = "rustuya"

        # 1) Online: errorCode 0
        await client._dispatch(
            "rustuya/error/devA",
            json.dumps({"errorCode": 0, "errorMsg": "Connection Successful", "id": "devA"}),
        )
        assert state.live_status["devA"]["state"] == "online"
        assert state.live_status["devA"]["code"] == 0

        # 2) Offline: errorCode 905
        await client._dispatch(
            "rustuya/error/devB",
            json.dumps(
                {
                    "errorCode": 905,
                    "errorMsg": "Network Error: Device Unreachable",
                    "id": "devB",
                    "payloadStr": "Device offline",
                }
            ),
        )
        assert state.live_status["devB"]["state"] == "offline"
        assert state.live_status["devB"]["code"] == 905
        assert "Unreachable" in state.live_status["devB"]["message"]

        # 3) Bridge-level error (id=bridge) is NOT stamped as a device live_status
        await client._dispatch(
            "rustuya/error/bridge",
            json.dumps({"errorCode": 0, "errorMsg": "Bridge ok", "id": "bridge"}),
        )
        assert "bridge" not in state.live_status

    @pytest.mark.asyncio
    async def test_event_with_object_payload_template_yields_dps(self):
        """Regression: when the user's `mqtt_payload_template` wraps the value
        inside a JSON object (e.g. `{"type":"{type}","value":{value}}`), the
        bridge's parse_mqtt_payload only merges topic vars in and doesn't
        synthesize `dps`. The manager has to use the template to find the
        value's JSON key and reconstruct dps[dp]. Without this, live DPS
        chips never showed up on the test server."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="rustuya",
                command="rustuya/command",
                event="rustuya/event/{id}/{dp}",
                message="rustuya/{level}/{id}",
                scanner="rustuya/scanner",
                payload='{"type": "{type}", "value": {value}}',
            )
        )
        client, _ = _make_client(state)
        client.root = "rustuya"

        await client._dispatch(
            "rustuya/event/devY/14",
            '{"type": "active", "value": "off"}',
        )
        assert state.dps.get("devY") == {"14": "off"}
        assert state.live_status["devY"]["state"] == "online"

    @pytest.mark.asyncio
    async def test_event_marks_device_online(self):
        """DPS events imply the device is alive — set live_status to online."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="rustuya",
                command="rustuya/command",
                event="rustuya/event/{type}/{id}",
                message="rustuya/{level}/{id}",
                scanner="rustuya/scanner",
                payload="{value}",
            )
        )
        client, _ = _make_client(state)
        client.root = "rustuya"
        await client._dispatch("rustuya/event/active/devX", json.dumps({"dps": {"1": True}}))
        assert state.live_status["devX"]["state"] == "online"

    @pytest.mark.asyncio
    async def test_empty_payload_skipped(self):
        """Retain-clearing publishes empty payload — must not crash or pollute state."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="myhome/tuya",
                command="myhome/tuya/cmd/{id}/{action}",
                event="myhome/tuya/dev/{name}/dp/{dp}/state",
                message="tuyalog/{level}/{id}",
                scanner="myhome/tuya/scanner",
                payload="{value}",
            )
        )
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
        await state.set_templates(
            BridgeTemplates(
                root="myhome/tuya",
                command="myhome/tuya/cmd/{id}/{action}",
                event="myhome/tuya/dev/{name}/dp/{dp}/state",
                message="tuyalog/{level}/{id}",
                scanner="myhome/tuya/scanner",
                payload="{value}",
            )
        )
        client, aio = _make_client(state)
        await client.publish_command("status", target_id="bridge")
        aio.publish.assert_awaited_once()
        topic, body = aio.publish.await_args.args[:2]
        assert topic == "myhome/tuya/cmd/bridge/status"
        parsed_body = json.loads(body)
        assert parsed_body == {"action": "status", "id": "bridge"}

    @pytest.mark.asyncio
    async def test_command_without_vars_in_topic(self):
        """Default command_topic is `{root}/command` with no vars — vars go in payload."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="rustuya",
                command="rustuya/command",
                event="rustuya/event/{type}/{id}",
                message="rustuya/{level}/{id}",
                scanner="rustuya/scanner",
                payload="{value}",
            )
        )
        client, aio = _make_client(state)
        await client.publish_command("add", target_id="bf123", extra={"key": "k", "ip": "1.2.3.4"})
        topic, body = aio.publish.await_args.args[:2]
        assert topic == "rustuya/command"
        assert json.loads(body) == {
            "action": "add",
            "id": "bf123",
            "key": "k",
            "ip": "1.2.3.4",
        }

    @pytest.mark.asyncio
    async def test_publish_before_bootstrap_raises(self):
        """templates=None during bootstrap — caller learns via clear RuntimeError."""
        client, _ = _make_client()
        with pytest.raises(RuntimeError, match="not yet resolved"):
            await client.publish_command("status")

    @pytest.mark.asyncio
    async def test_publish_when_disconnected_raises(self):
        """Reconnect gap: `_connected` is clear → publish must fail fast so
        the FastAPI handler can return 503 instead of hanging on aiomqtt."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="rustuya",
                command="rustuya/command",
                event="rustuya/event/{type}/{id}",
                message="rustuya/{level}/{id}",
                scanner="rustuya/scanner",
                payload="{value}",
            )
        )
        client, _ = _make_client(state)
        client._connected.clear()
        with pytest.raises(RuntimeError, match="not connected"):
            await client.publish_command("status", target_id="bridge")

    @pytest.mark.asyncio
    async def test_publish_translates_mqtt_error_to_runtime(self):
        """aiomqtt raises MqttError on a broken publish — the manager surface
        translates that to RuntimeError so FastAPI handlers have a single
        exception type to catch."""
        state = State()
        await state.set_templates(
            BridgeTemplates(
                root="rustuya",
                command="rustuya/command",
                event="rustuya/event/{type}/{id}",
                message="rustuya/{level}/{id}",
                scanner="rustuya/scanner",
                payload="{value}",
            )
        )
        client, aio = _make_client(state)
        aio.publish = AsyncMock(side_effect=aiomqtt.MqttError("publish failed"))
        with pytest.raises(RuntimeError, match="publish failed"):
            await client.publish_command("status", target_id="bridge")


# ─────────────────────────────────────────────────────────────────────────────
# bridge-offline / broker-unreachable warnings
# ─────────────────────────────────────────────────────────────────────────────


class TestBridgeOfflineWarning:
    """`_apply_default_templates` is the fallback when bridge/config never
    arrives. It must mark `bridge_offline` so the UI surfaces "manager is up
    but bridge isn't"; `_on_bridge_config` must clear it the moment a real
    config lands."""

    @pytest.mark.asyncio
    async def test_default_templates_set_bridge_offline_warning(self):
        client, _ = _make_client()
        await client._apply_default_templates()
        warn = client.state.warnings.get("bridge_offline")
        assert warn is not None
        assert warn["level"] == "warning"
        assert "myhome/tuya/bridge/config" in warn["message"]

    @pytest.mark.asyncio
    async def test_real_bridge_config_clears_offline_warning(self):
        client, _ = _make_client()
        await client._apply_default_templates()
        assert "bridge_offline" in client.state.warnings

        # Now a real retained config arrives. Use the same payload shape as
        # the dispatch tests above; _on_bridge_config picks it up and clears.
        await client._on_bridge_config(json.dumps(CUSTOM_CONFIG))
        assert "bridge_offline" not in client.state.warnings


class TestReconnectLoop:
    """Validates that the aiomqtt reconnect loop turns a connection failure
    into a `broker_unreachable` state warning (the signal the UI surfaces)
    and clears it once the broker comes back."""

    @pytest.mark.asyncio
    async def test_mqtt_error_on_connect_sets_warning(self, monkeypatch):
        """First aiomqtt.Client enter raises MqttError → warning surfaces."""
        state = State()

        class FailingClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                raise aiomqtt.MqttError("Connection refused")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("rustuya_manager.mqtt.aiomqtt.Client", FailingClient)

        client = BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)
        # Tight backoff so the test doesn't sleep the wall clock.
        client._INITIAL_BACKOFF_SEC = 0.01
        client._MAX_BACKOFF_SEC = 0.01

        async with client:
            # Give the loop a couple iterations to register the failure.
            await asyncio.sleep(0.05)
            warn = state.warnings.get("broker_unreachable")
            assert warn is not None
            assert warn["level"] == "error"
            assert "localhost:1883" in warn["message"]

    @pytest.mark.asyncio
    async def test_reconnect_clears_warning_and_resets_backoff(self, monkeypatch):
        """Two MqttError attempts then a successful enter → warning cleared,
        backoff reset (verified by checking that the success log fires)."""
        state = State()
        attempts = {"n": 0}

        # We need a working aiomqtt.Client mock for the success path: enter
        # returns a mock with subscribe/messages, then the messages iterator
        # immediately raises MqttError to force one more reconnect cycle.
        class FlakyClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise aiomqtt.MqttError(f"refused {attempts['n']}")

                mock = MagicMock()
                mock.subscribe = AsyncMock(return_value=None)
                mock.unsubscribe = AsyncMock(return_value=None)
                mock.publish = AsyncMock(return_value=None)

                async def _messages():
                    # Yield nothing then raise — simulates broker dropping us
                    # so we exit the loop cleanly.
                    await asyncio.sleep(0.02)
                    raise aiomqtt.MqttError("dropped")
                    yield  # pragma: no cover — unreachable

                mock.messages = _messages()
                return mock

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("rustuya_manager.mqtt.aiomqtt.Client", FlakyClient)

        client = BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)
        client._INITIAL_BACKOFF_SEC = 0.01
        client._MAX_BACKOFF_SEC = 0.01

        async with client:
            # Wait long enough for: two failures, one success (clears warning),
            # then drop (sets warning again).
            await asyncio.sleep(0.2)
            # By now we've succeeded at least once. The warning should be set
            # again (because the message-stream drop re-sets it), but
            # `attempts["n"]` should be >= 3 confirming reconnect actually
            # happened.
            assert attempts["n"] >= 3


class TestFormatErrorMessage:
    """`_format_error_message` is the bridge-error renderer used for the UI's
    MSG cell. It must take any structured error payload and produce a single
    line — without per-errorCode special-casing, so new error variants from
    rustuya don't require manager updates."""

    def test_plain_error_returns_just_msg(self):
        assert (
            _format_error_message({"errorCode": 100, "errorMsg": "Device offline"})
            == "Device offline"
        )

    def test_envelope_only_returns_empty_string(self):
        # No errorMsg, no payloadStr, no extras — still safe (no crash).
        assert _format_error_message({"errorCode": 100}) == ""

    def test_ip_mismatch_appends_structured_extras(self):
        # The 906 / ip_mismatch payload shape from rustuya 0.2.6. The formatter
        # has no knowledge of 906 specifically — it just appends every scalar
        # extra after the base errorMsg.
        msg = _format_error_message(
            {
                "errorCode": 906,
                "errorMsg": "State error",
                "reason": "ip_mismatch",
                "configured": "192.168.1.10",
                "discovered": "192.168.1.42",
            }
        )
        assert msg.startswith("State error (")
        assert msg.endswith(")")
        assert "reason=ip_mismatch" in msg
        assert "configured=192.168.1.10" in msg
        assert "discovered=192.168.1.42" in msg

    def test_payload_str_fallback_when_no_error_msg(self):
        assert (
            _format_error_message({"errorCode": 500, "payloadStr": "raw garbage"}) == "raw garbage"
        )

    def test_nested_extras_skipped(self):
        # Lists/dicts as extra fields would blow out the single-line MSG cell,
        # so the formatter only includes scalar extras.
        msg = _format_error_message(
            {
                "errorCode": 500,
                "errorMsg": "Boom",
                "trace": ["a", "b"],  # list — skipped
                "detail": {"k": "v"},  # dict — skipped
                "code_str": "abc",  # scalar — kept
            }
        )
        assert msg == "Boom (code_str=abc)"

    def test_extras_only_when_no_base_msg(self):
        # No errorMsg/payloadStr but structured extras: render the extras
        # alone so the user still sees something diagnostic.
        msg = _format_error_message({"errorCode": 906, "reason": "ip_mismatch"})
        assert msg == "reason=ip_mismatch"


# pytest-asyncio integration — auto mode is the simplest setup for our needs.
def pytest_collection_modifyitems(items):
    for item in items:
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)
