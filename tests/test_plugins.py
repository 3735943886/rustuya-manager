"""Tests for the universal plugin host.

Two layers:
  - the host surface itself (registry/ctx/dispatch/manifest), driven by fake
    plugins injected via `build_app(..., plugins=[register])` or a direct
    `PluginContext` — no installed entry point required;
  - the no-regression guarantees that a plugin-less manager is byte-identical
    on the wire and in the rendered HTML.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from rustuya_manager.models import Device
from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.plugins import (
    PLUGIN_API_VERSION,
    PluginContext,
    PluginRegistry,
    topic_matches,
)
from rustuya_manager.state import State
from rustuya_manager.web import build_app, serialize_state


def _make_client(state: State) -> BridgeClient:
    # No async context entered: _client stays None, so add_plugin_subscription
    # only caches (no immediate subscribe), which is all these tests need.
    return BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)


# ── topic_matches ────────────────────────────────────────────────────────
class TestTopicMatches:
    @pytest.mark.parametrize(
        "filter_,topic,expected",
        [
            ("a/b/c", "a/b/c", True),
            ("a/b/c", "a/b/d", False),
            ("a/+/c", "a/b/c", True),
            ("a/+/c", "a/b/d/c", False),
            ("a/+/c", "a/c", False),
            ("homeassistant/#", "homeassistant/light/x/config", True),
            ("homeassistant/#", "homeassistant", True),  # # swallows zero levels
            ("a/#", "b/c", False),
            ("a/b", "a/b/c", False),
            ("+", "a", True),
            ("+", "a/b", False),
        ],
    )
    def test_match(self, filter_, topic, expected):
        assert topic_matches(filter_, topic) is expected


# ── (a) API router inclusion ─────────────────────────────────────────────
def test_plugin_router_is_included():
    def register(ctx):
        router = APIRouter()

        @router.get("/api/hello")
        async def hello() -> dict:
            return {"ok": True, "api_version": ctx.api_version}

        ctx.add_api_router(router)

    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugins=[register])) as tc:
        r = tc.get("/api/hello")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "api_version": PLUGIN_API_VERSION}


# ── (b) MQTT subscription routing ────────────────────────────────────────
async def test_plugin_mqtt_handler_receives_matching_messages():
    state = State()
    client = _make_client(state)
    received: list[tuple[str, str, bool]] = []

    async def handler(topic: str, payload: str, retain: bool) -> None:
        received.append((topic, payload, retain))

    client.add_plugin_subscription("hello/#", handler)

    # Matching topic → handler invoked with (topic, payload, retain). Routing
    # happens before the bridge-template guard, so templates being None (no
    # bootstrap) does not suppress it.
    await client._dispatch("hello/world", '{"a":1}', retain=True)
    # Non-matching topic → handler not invoked.
    await client._dispatch("other/topic", "x", retain=False)

    assert received == [("hello/world", '{"a":1}', True)]


async def test_add_mqtt_subscription_via_ctx_records_and_routes():
    state = State()
    client = _make_client(state)
    registry = PluginRegistry()
    ctx = PluginContext(registry, bridge_client=client, state=state)
    received: list[tuple[str, str, bool]] = []

    async def handler(topic: str, payload: str, retain: bool) -> None:
        received.append((topic, payload, retain))

    ctx.add_mqtt_subscription("hello/#", handler)
    assert registry.mqtt_subscriptions == [("hello/#", handler)]

    await client._dispatch("hello/x", "p", retain=False)
    assert received == [("hello/x", "p", False)]


# ── (c) State namespace ──────────────────────────────────────────────────
async def test_state_namespace_bumps_and_serializes():
    state = State()
    client = _make_client(state)
    ctx = PluginContext(PluginRegistry(), bridge_client=client, state=state)

    ns = ctx.state_namespace("hello")
    assert ns.get() is None
    v0 = state.version

    await ns.set({"pings": 3})

    assert state.version > v0  # WS broadcast is triggered
    assert ns.get() == {"pings": 3}
    snap = serialize_state(state)
    assert snap["plugins"] == {"hello": {"pings": 3}}


# ── (d) Page manifest + static serving ───────────────────────────────────
def test_api_plugins_manifest_and_static(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.js").write_text("export function mount() {}")

    def register(ctx):
        ctx.add_page("hello", "Hello", static_dir=str(static_dir))

    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugins=[register])) as tc:
        manifest = tc.get("/api/plugins").json()
        assert manifest == [{"id": "hello", "label": "Hello", "js_url": "/plugins/hello/index.js"}]
        served = tc.get("/plugins/hello/index.js")
        assert served.status_code == 200
        assert "mount" in served.text


# ── (e) Read-only snapshots: devices() + bridge_config() ─────────────────
async def test_devices_returns_raw_data_snapshot():
    state = State()
    raw = {"id": "dev1", "name": "Lamp", "product_id": "abc", "local_key": "k"}
    await state.set_cloud({"dev1": Device.from_dict(raw)})

    ctx = PluginContext(PluginRegistry(), bridge_client=_make_client(state), state=state)
    snap = ctx.devices()
    assert snap == {"dev1": raw}

    # Fresh outer dict each call — mutating it must not corrupt the manager's map.
    snap["dev2"] = {}
    assert "dev2" not in state.cloud
    assert set(ctx.devices()) == {"dev1"}


def test_devices_empty_before_cloud_load():
    state = State()
    ctx = PluginContext(PluginRegistry(), bridge_client=_make_client(state), state=state)
    assert ctx.devices() == {}


async def test_bridge_config_none_then_copy():
    state = State()
    ctx = PluginContext(PluginRegistry(), bridge_client=_make_client(state), state=state)
    assert ctx.bridge_config() is None

    cfg = {"mqtt_root_topic": "rustuya", "mqtt_retain": True}
    await state.set_bridge_config_raw(cfg)

    got = ctx.bridge_config()
    assert got == cfg
    # Shallow copy — mutating the returned dict must not touch the stored one.
    got["mqtt_root_topic"] = "hacked"
    assert state.bridge_config_raw["mqtt_root_topic"] == "rustuya"


async def test_set_bridge_config_raw_does_not_bump_version():
    # set_templates already broadcasts the same config change; storing the raw
    # dict must not trigger a second redundant WS push.
    state = State()
    v0 = state.version
    await state.set_bridge_config_raw({"mqtt_root_topic": "rustuya"})
    assert state.version == v0


# ── (f) publish_raw ──────────────────────────────────────────────────────
async def test_publish_raw_when_disconnected_raises():
    client = _make_client(State())  # never entered → _connected not set
    with pytest.raises(RuntimeError, match="not connected"):
        await client.publish_raw("homeassistant/light/x/config", "{}", retain=True)


async def test_publish_raw_forwards_topic_payload_and_flags():
    client = _make_client(State())

    class _FakeMqtt:
        def __init__(self):
            self.calls = []

        async def publish(self, topic, payload, *, qos, retain):
            self.calls.append((topic, payload, qos, retain))

    fake = _FakeMqtt()
    client._client = fake  # type: ignore[assignment]
    client._connected.set()

    await client.publish_raw("homeassistant/light/x/config", '{"a":1}', retain=True)
    await client.publish_raw("homeassistant/light/x/config", "", retain=True)  # clear

    assert fake.calls == [
        ("homeassistant/light/x/config", '{"a":1}', 1, True),
        ("homeassistant/light/x/config", "", 1, True),
    ]


# ── Failure isolation ────────────────────────────────────────────────────
def test_broken_plugin_does_not_break_app():
    def good(ctx):
        router = APIRouter()

        @router.get("/api/good")
        async def good_ep() -> dict:
            return {"ok": True}

        ctx.add_api_router(router)

    def broken(ctx):
        raise RuntimeError("boom")

    state = State()
    client = _make_client(state)
    # broken's register() raises; the host logs + skips it, good still loads.
    with TestClient(build_app(state, client, plugins=[broken, good])) as tc:
        assert tc.get("/api/good").json() == {"ok": True}
        assert tc.get("/api/state").status_code == 200


# ── No-regression: zero plugins is byte-identical ────────────────────────
class TestZeroPluginNoRegression:
    def test_serialize_omits_plugins_key_when_empty(self):
        snap = serialize_state(State())
        assert "plugins" not in snap

    def test_api_plugins_empty_without_plugins(self):
        state = State()
        client = _make_client(state)
        with TestClient(build_app(state, client)) as tc:
            assert tc.get("/api/plugins").json() == []

    def test_index_html_has_no_tab_bar(self):
        state = State()
        client = _make_client(state)
        with TestClient(build_app(state, client)) as tc:
            html = tc.get("/").text
            # The tab bar is built client-side only when ≥1 plugin exists; it
            # must never be present in the served template.
            assert 'id="page-tabs"' not in html
            assert 'id="plugin-page-root"' not in html

    def test_plugins_js_served(self):
        state = State()
        client = _make_client(state)
        with TestClient(build_app(state, client)) as tc:
            r = tc.get("/static/plugins.js")
            assert r.status_code == 200
            assert "initPluginHost" in r.text
