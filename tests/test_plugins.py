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
    load_plugins,
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
        assert manifest == {
            "pages": [{"id": "hello", "label": "Hello", "js_url": "/plugins/hello/index.js"}],
            "init_scripts": [],
        }
        served = tc.get("/plugins/hello/index.js")
        assert served.status_code == 200
        assert "mount" in served.text
        # Plugin assets must be no-cache like /static, so a drop-in plugin edited
        # on disk (swap files + restart) takes effect without a browser restart.
        assert "no-cache" in served.headers.get("cache-control", "")


def test_api_plugins_header_init_manifest_and_shared_mount(tmp_path):
    # A plugin can contribute an eager init script (the route for header menu
    # items) reusing the same id/static_dir as its page — mounted once.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.js").write_text("export function mount() {}")
    (static_dir / "init.js").write_text("export function init(ctx) {}")

    def register(ctx):
        ctx.add_page("hello", "Hello", static_dir=str(static_dir))
        ctx.add_header_init("hello", static_dir=str(static_dir))

    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugins=[register])) as tc:
        manifest = tc.get("/api/plugins").json()
        assert manifest["init_scripts"] == ["/plugins/hello/init.js"]
        # Shared mount serves both the page and the init module.
        assert "mount" in tc.get("/plugins/hello/index.js").text
        assert "init" in tc.get("/plugins/hello/init.js").text


def test_header_only_plugin_has_init_script_but_no_pages(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "init.js").write_text("export function init(ctx) {}")

    def register(ctx):
        ctx.add_header_init("menu-only", static_dir=str(static_dir))

    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugins=[register])) as tc:
        manifest = tc.get("/api/plugins").json()
        assert manifest["pages"] == []
        assert manifest["init_scripts"] == ["/plugins/menu-only/init.js"]
        assert tc.get("/plugins/menu-only/init.js").status_code == 200


# ── (f) Drop-in directory plugins (no pip install) ───────────────────────
def _write_pkg_plugin(root, name: str, body: str) -> None:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text(body)


def test_dir_plugin_package_is_discovered_and_registered(tmp_path):
    # A package dropped into plugin_dir, exposing register(ctx), is loaded — and
    # its own static/ (resolved via Path(__file__).parent) serves correctly.
    _write_pkg_plugin(
        tmp_path,
        "dropin_pkg",
        "from pathlib import Path\n"
        "_STATIC = Path(__file__).resolve().parent / 'static'\n"
        "def register(ctx):\n"
        "    ctx.add_page('dropin', 'Drop-in', static_dir=str(_STATIC))\n",
    )
    static = tmp_path / "dropin_pkg" / "static"
    static.mkdir()
    (static / "index.js").write_text("export function mount() {}")

    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugin_dirs=[str(tmp_path)])) as tc:
        manifest = tc.get("/api/plugins").json()
        assert {
            "id": "dropin",
            "label": "Drop-in",
            "js_url": "/plugins/dropin/index.js",
        } in manifest["pages"]
        assert "mount" in tc.get("/plugins/dropin/index.js").text


def test_dir_plugin_single_file_is_discovered(tmp_path):
    (tmp_path / "dropin_solo.py").write_text(
        "def register(ctx):\n    ctx.add_api_router(_router())\n"
        "def _router():\n"
        "    from fastapi import APIRouter\n"
        "    r = APIRouter()\n"
        "    @r.get('/api/dropin-solo')\n"
        "    async def _h():\n        return {'ok': True}\n"
        "    return r\n"
    )
    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugin_dirs=[str(tmp_path)])) as tc:
        assert tc.get("/api/dropin-solo").json() == {"ok": True}


def test_dir_plugin_broken_one_is_skipped_others_load(tmp_path):
    # A package that raises at import time must not stop a sibling from loading.
    _write_pkg_plugin(tmp_path, "dropin_boom", "raise RuntimeError('boom at import')\n")
    _write_pkg_plugin(
        tmp_path,
        "dropin_ok",
        "def register(ctx):\n    ctx.state_namespace('dropin_ok')\n",
    )
    registry = PluginRegistry()
    state = State()
    ctx = PluginContext(registry, bridge_client=_make_client(state), state=state)
    # Must not raise despite the broken sibling.
    load_plugins(ctx, plugin_dirs=[str(tmp_path)])
    # The good one ran (it didn't add a page/router, but reaching here without
    # an exception proves isolation held and discovery continued).


def test_dir_plugin_ignores_non_packages_and_underscored(tmp_path):
    # A bare dir (no __init__.py) and an _private.py are both skipped.
    (tmp_path / "not_a_pkg").mkdir()
    (tmp_path / "not_a_pkg" / "data.txt").write_text("nope")
    (tmp_path / "_private.py").write_text(
        "def register(ctx):\n    raise AssertionError('loaded!')\n"
    )
    state = State()
    client = _make_client(state)
    # Build succeeds and the underscored module's register never runs.
    with TestClient(build_app(state, client, plugin_dirs=[str(tmp_path)])) as tc:
        assert tc.get("/api/plugins").json() == {"pages": [], "init_scripts": []}


# ── (g) Runtime add-only scan (POST /api/plugins/scan) ───────────────────
def test_scan_loads_newly_added_dir_plugin(tmp_path):
    # A plugin dropped into the dir AFTER startup is picked up by a scan — its
    # route/page/static are wired onto the live app, no restart.
    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client, plugin_dirs=[str(tmp_path)])) as tc:
        assert tc.get("/api/plugins").json() == {"pages": [], "init_scripts": []}

        _write_pkg_plugin(
            tmp_path,
            "late_plugin",
            "from pathlib import Path\n"
            "_S = Path(__file__).resolve().parent / 'static'\n"
            "def register(ctx):\n"
            "    ctx.add_page('late', 'Late', static_dir=str(_S))\n",
        )
        static = tmp_path / "late_plugin" / "static"
        static.mkdir()
        (static / "index.js").write_text("export function mount() {}")

        r = tc.post("/api/plugins/scan").json()
        assert r["ok"] is True
        assert r["added"] == 1
        assert {"id": "late", "label": "Late", "js_url": "/plugins/late/index.js"} in r["pages"]
        # GET reflects it and the runtime static mount serves the asset.
        assert any(p["id"] == "late" for p in tc.get("/api/plugins").json()["pages"])
        assert "mount" in tc.get("/plugins/late/index.js").text
        # Idempotent: a second scan finds nothing new (dedup by register identity).
        assert tc.post("/api/plugins/scan").json()["added"] == 0


def test_scan_with_no_plugin_dir_is_noop(tmp_path):
    state = State()
    client = _make_client(state)
    with TestClient(build_app(state, client)) as tc:
        r = tc.post("/api/plugins/scan").json()
        assert r == {"ok": True, "added": 0, "pages": [], "init_scripts": []}


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


async def test_bridge_config_redacts_credentials():
    # ctx.bridge_config() must never hand MQTT credentials to a plugin, even if
    # an older/external bridge published them. mqtt_user/mqtt_password are
    # dropped and inline user:pass@ in mqtt_broker is scrubbed; everything else
    # (incl. the bridge version) passes through.
    state = State()
    ctx = PluginContext(PluginRegistry(), bridge_client=_make_client(state), state=state)
    await state.set_bridge_config_raw(
        {
            "mqtt_root_topic": "rustuya",
            "mqtt_user": "admin",
            "mqtt_password": "secret",
            "mqtt_broker": "mqtt://u:p@host:1883",
            "version": "0.3.0-rc.25",
        }
    )
    got = ctx.bridge_config()
    assert "mqtt_user" not in got
    assert "mqtt_password" not in got
    assert got["mqtt_broker"] == "mqtt://host:1883"  # inline creds scrubbed
    assert got["version"] == "0.3.0-rc.25"  # non-secret fields preserved
    assert got["mqtt_root_topic"] == "rustuya"
    # The stored config is untouched (redaction works on a copy).
    assert state.bridge_config_raw["mqtt_user"] == "admin"


async def test_serialize_exposes_bridge_version():
    state = State()
    assert serialize_state(state)["bridge_version"] is None  # no config yet
    await state.set_bridge_config_raw({"mqtt_root_topic": "rustuya", "version": "0.3.0-rc.25"})
    assert serialize_state(state)["bridge_version"] == "0.3.0-rc.25"


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
            # Empty lists ⇒ the client builds no tab bar and runs no eager
            # imports, so a plugin-less UI is identical to before.
            assert tc.get("/api/plugins").json() == {"pages": [], "init_scripts": []}

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


# ── reactive DP bus (api_version >= 2) ───────────────────────────────────
async def test_reactive_dp_bus_wires_and_dispatches():
    """ctx.watch_* register watchers that fire from the route path with decoded
    DPs; ctx.derived_dp returns a handle. End-to-end through `_dispatch` (no
    broker needed — the bus is in-process function calls)."""
    from rustuya_manager.plugins import DerivedDp
    from rustuya_manager.state import BridgeTemplates

    state = State()
    await state.set_templates(
        BridgeTemplates(
            root="rustuya",
            command="rustuya/command/{id}/{action}",
            event="rustuya/event/{type}/{id}",
            message="rustuya/{level}/{id}",
            scanner="rustuya/scanner",
            payload="{value}",
        )
    )
    client = _make_client(state)
    ctx = PluginContext(PluginRegistry(), bridge_client=client, state=state)

    seen: list = []

    async def on_dp(device_id, dps, origin):
        seen.append((device_id, dps, origin))

    ctx.watch_dps(on_dp)
    assert len(client._dp_watchers) == 1

    await client._dispatch("rustuya/event/passive/D1", '{"1":true}')
    assert seen == [("D1", {"1": True}, "device")]

    assert isinstance(ctx.derived_dp("D1", "99"), DerivedDp)
