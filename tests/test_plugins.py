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
