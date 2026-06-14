"""Universal, HA-agnostic plugin host for rustuya-manager.

A plugin is any installed package that exposes a `register(ctx)` callable under
the `rustuya_manager.plugins` entry-point group. At startup `build_app` discovers
them (stdlib `importlib.metadata` only — no extra runtime dependency) and calls
`register(ctx)` once each. Through `ctx` a plugin can contribute four things:

  1. a FastAPI `APIRouter`            (ctx.add_api_router)
  2. an MQTT subscription + handler   (ctx.add_mqtt_subscription)
  3. a state namespace               (ctx.state_namespace) — rides the existing
                                      WS broadcast for free
  4. a UI page (tab + static assets) (ctx.add_page)

It can also *read* two host-owned snapshots — the cloud devices
(`ctx.devices`) and the raw bridge config (`ctx.bridge_config`) — and publish
arbitrary retained payloads via `ctx.bridge_client.publish_raw`. These let a
plugin re-derive its own view of the fleet without the host knowing what for.

The host knows nothing about what any plugin does. Discovery and every
`register()` call are wrapped so a broken or malicious plugin is logged and
skipped — the manager always keeps running. With **zero** plugins installed the
host adds nothing observable: no `plugins` key in the WS snapshot, no UI tab bar,
no behavioural change at all.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .mqtt import BridgeClient
    from .state import State

logger = logging.getLogger(__name__)

# Bumped only on a breaking change to the `ctx` contract below. Plugins read it
# via `ctx.api_version` to refuse to load against an incompatible host.
PLUGIN_API_VERSION = 1

# Entry-point group plugins advertise their `register(ctx)` callable under.
ENTRY_POINT_GROUP = "rustuya_manager.plugins"

# An MQTT message handler: async (topic, payload, retain) -> None.
MqttHandler = Callable[[str, str, bool], Awaitable[None]]

# Credential fields stripped from the bridge config before it's handed to a
# plugin via ctx.bridge_config(). Matches the bridge's own skip_serializing set
# (rc25+) so the manager redacts identically even against an older/external
# bridge that still publishes them.
_REDACTED_CONFIG_KEYS = frozenset({"mqtt_user", "mqtt_password"})


def _scrub_broker_url(url: str) -> str:
    """Drop an inline `user[:pass]@` segment from a broker URL, keeping the
    scheme + host:port. The bridge documents that credentials embedded directly
    in `mqtt_broker` still serialize even after rc25's field redaction, so this
    closes that residual leak path on the manager side."""
    if "@" not in url:
        return url
    scheme = ""
    rest = url
    if "://" in url:
        scheme, rest = url.split("://", 1)
        scheme += "://"
    # Strip everything up to and including the last '@' (host/port is the tail).
    _, _, host = rest.rpartition("@")
    return scheme + host


def topic_matches(filter_: str, topic: str) -> bool:
    """Return True if MQTT topic `topic` matches subscription filter `filter_`.

    Implements the two MQTT wildcards so plugins can subscribe the same way they
    would against any broker, without pulling in paho's matcher (and its
    version-specific import path):
      `+`  matches exactly one topic level
      `#`  matches the rest of the topic (must be the final level)

    Examples: `homeassistant/#` matches `homeassistant/light/x/config`;
    `a/+/c` matches `a/b/c` but not `a/b/d/c`.
    """
    f_parts = filter_.split("/")
    t_parts = topic.split("/")
    for i, fp in enumerate(f_parts):
        if fp == "#":
            # `#` is only legal as the final level; it swallows the remainder
            # (including zero levels, per the MQTT spec).
            return True
        if i >= len(t_parts):
            return False
        if fp == "+":
            continue
        if fp != t_parts[i]:
            return False
    # All filter levels consumed — match only if the topic had no extra levels.
    return len(f_parts) == len(t_parts)


class PluginRegistry:
    """Accumulates everything plugins contribute during their `register(ctx)`.

    `build_app` reads this after discovery to include routers, expose the page
    manifest, and mount per-plugin static directories. The MQTT subscriptions
    are also handed to the live `BridgeClient` as they are registered (see
    `PluginContext.add_mqtt_subscription`); they are kept here too purely for
    introspection/tests.
    """

    def __init__(self) -> None:
        self.api_routers: list[APIRouter] = []
        self.mqtt_subscriptions: list[tuple[str, MqttHandler]] = []
        self.pages: list[dict[str, Any]] = []
        # Eagerly-loaded JS modules (each {id, static_dir, entry}). Unlike pages
        # (mounted lazily when their tab opens), these are imported at boot so a
        # plugin can contribute always-visible UI — e.g. a header menu item via
        # ctx.addHeaderAction — without the user ever opening its tab.
        self.init_scripts: list[dict[str, Any]] = []


class StateNamespace:
    """A plugin's private slice of `State`, broadcast over the existing WS.

    `set()` stores the data under the plugin's namespace and bumps the State
    version, so the manager's WebSocket loop pushes it to every client with no
    plugin-specific code on the broadcast path. The frontend reads it back at
    `snapshot.plugins[<name>]`.
    """

    def __init__(self, state: State, name: str) -> None:
        self._state = state
        self._name = name

    async def set(self, data: dict[str, Any]) -> None:
        await self._state.set_plugin_data(self._name, data)

    def get(self) -> dict[str, Any] | None:
        return self._state.get_plugin_data(self._name)


class PluginContext:
    """The single object handed to every plugin's `register(ctx)`.

    Intentionally small and host-agnostic — it exposes the four contribution
    surfaces, two read-only snapshots (`devices`, `bridge_config`), the API
    version, and the live `BridgeClient` (for publishing). Plugins must not
    reach past this into manager internals.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        *,
        bridge_client: BridgeClient,
        state: State,
    ) -> None:
        self._registry = registry
        self._state = state
        self.api_version = PLUGIN_API_VERSION
        self.bridge_client = bridge_client

    def devices(self) -> dict[str, dict[str, Any]]:
        """Read-only snapshot of the cloud devices as `{id: raw_data}`.

        `raw_data` is the original per-device dict the manager loaded from the
        cloud JSON (the same shape a plugin would feed to a discovery
        generator). Returns a fresh outer dict each call so a plugin can't
        mutate the manager's device map; the inner `raw_data` dicts are shared
        by reference (not deep-copied — they can be large and plugins are
        expected to read, not write). Empty until the cloud snapshot loads."""
        return {did: dev.raw_data for did, dev in self._state.cloud.items()}

    def bridge_config(self) -> dict[str, Any] | None:
        """Read-only, credential-redacted copy of the raw `{root}/bridge/config`
        payload dict, or None if the bridge config hasn't been received yet.

        These are the bridge's original config keys (`mqtt_root_topic`,
        `mqtt_event_topic`, `mqtt_payload_template`, `mqtt_retain`, `version`, …)
        before `{root}` substitution — what a plugin needs to re-derive its own
        view of the bridge's topic/payload scheme.

        MQTT credentials are stripped before the dict reaches a plugin:
        `mqtt_user`/`mqtt_password` are dropped and any inline `user:pass@` in
        `mqtt_broker` is scrubbed. Bridge >= 0.2.0rc25 already omits the
        credential fields from the published config, but the manager redacts
        here too so an older or external standalone bridge can't leak its
        broker credentials through the plugin surface regardless of its version.
        A fresh dict is returned so the caller can't mutate the stored config."""
        cfg = self._state.bridge_config_raw
        if cfg is None:
            return None
        safe = {k: v for k, v in cfg.items() if k not in _REDACTED_CONFIG_KEYS}
        broker = safe.get("mqtt_broker")
        if isinstance(broker, str):
            safe["mqtt_broker"] = _scrub_broker_url(broker)
        return safe

    def add_api_router(self, router: APIRouter) -> None:
        self._registry.api_routers.append(router)

    def add_mqtt_subscription(self, topic_filter: str, handler: MqttHandler) -> None:
        self._registry.mqtt_subscriptions.append((topic_filter, handler))
        # Hand to the live client so it subscribes now (if connected) and
        # replays on every reconnect.
        self.bridge_client.add_plugin_subscription(topic_filter, handler)

    def state_namespace(self, name: str) -> StateNamespace:
        return StateNamespace(self._state, name)

    def add_page(
        self,
        id: str,
        label: str,
        *,
        static_dir: str,
        entry: str = "index.js",
    ) -> None:
        self._registry.pages.append(
            {"id": id, "label": label, "static_dir": static_dir, "entry": entry}
        )

    def add_header_init(
        self,
        id: str,
        *,
        static_dir: str,
        entry: str = "init.js",
    ) -> None:
        """Register a JS module loaded eagerly at UI boot (not lazily like a page).

        Its `static_dir` is served under `/plugins/{id}/` exactly like a page's
        (a plugin may reuse the same `id`/`static_dir` for both a page and its
        init script — the host mounts each id once). The module should export
        `init(ctx)`, which runs at boot with the same context a page mount gets,
        plus `ctx.addHeaderAction(...)` to contribute hamburger-menu items. This
        is the route for always-visible plugin UI — the item shows up without the
        user opening the plugin's tab."""
        self._registry.init_scripts.append({"id": id, "static_dir": static_dir, "entry": entry})


def load_plugins(
    ctx: PluginContext,
    *,
    register_callables: list[Callable[[PluginContext], None]] | None = None,
) -> None:
    """Discover and register all plugins into `ctx`.

    `register_callables` lets callers (tests, or an explicit `build_app(...,
    plugins=[...])`) inject `register` functions without an installed entry
    point; they run in addition to anything discovered via the entry-point
    group. Discovery, loading, and each `register()` call are individually
    guarded so one bad plugin can never take the manager down.
    """
    registers: list[Callable[[PluginContext], None]] = list(register_callables or [])

    try:
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - importlib.metadata shape varies; never fatal
        logger.exception("plugin discovery failed; continuing with no discovered plugins")
        eps = []

    for ep in eps:
        try:
            registers.append(ep.load())
        except Exception:  # noqa: BLE001 - a broken plugin must not abort the rest
            logger.exception(
                "failed to load plugin entry point %r; skipping", getattr(ep, "name", ep)
            )

    for reg in registers:
        try:
            reg(ctx)
        except Exception:  # noqa: BLE001 - register() failure is isolated per plugin
            logger.exception("plugin register(ctx) raised; skipping that plugin")
