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

    Intentionally small and host-agnostic — it exposes exactly the four
    contribution surfaces plus the API version and the live `BridgeClient`
    (for publishing). Plugins must not reach past this into manager internals.
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
