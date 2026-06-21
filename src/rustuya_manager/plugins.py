"""Universal, HA-agnostic plugin host for rustuya-manager.

A plugin is anything exposing a `register(ctx)` callable, found one of two ways:
  - an installed package under the `rustuya_manager.plugins` entry-point group
    (stdlib `importlib.metadata` only — no extra runtime dependency); or
  - a package/module dropped into a `--plugin-dir` (e.g. a mounted Docker
    `/data/plugins`), loaded by `_discover_dir_plugins` — no pip install needed.

At startup `build_app` discovers both and calls `register(ctx)` once each.
Through `ctx` a plugin can contribute five things:

  1. a FastAPI `APIRouter`            (ctx.add_api_router)
  2. an MQTT subscription + handler   (ctx.add_mqtt_subscription)
  3. a state namespace               (ctx.state_namespace) — rides the existing
                                      WS broadcast for free
  4. a UI page (tab + static assets) (ctx.add_page)
  5. an eager init module            (ctx.add_header_init) — runs at boot, e.g.
                                      to add a hamburger-menu item
  6. reactive DP handlers            (ctx.watch_dps / watch_device / watch_dp)
                                      + derived-DP output (ctx.derived_dp) and
                                      device control (ctx.set_device_dp) — the
                                      in-process DP bus, api_version >= 2

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

import asyncio
import importlib
import importlib.metadata
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .mqtt import BridgeClient
    from .state import State

logger = logging.getLogger(__name__)

# Incremented when the `ctx` contract gains or breaks surface; plugins read it
# via `ctx.api_version` (compare `>=`) to refuse to load against a host too old
# for what they need. v2 added the reactive DP bus: watch_dps/watch_device/
# watch_dp, derived_dp, set_device_dp.
PLUGIN_API_VERSION = 2

# Entry-point group plugins advertise their `register(ctx)` callable under.
ENTRY_POINT_GROUP = "rustuya_manager.plugins"

# An MQTT message handler: async (topic, payload, retain) -> None.
MqttHandler = Callable[[str, str, bool], Awaitable[None]]

# A DP watcher: async (device_id, dps, origin) -> None. `dps` is the decoded
# {dp: value} delta from one device event; `origin` is "device".
DpWatcher = Callable[[str, "dict[str, Any]", str], Awaitable[None]]

# A plugin service: a zero-arg factory returning the long-lived coroutine the
# manager supervises (started after bootstrap, crash-backoff restart, cancelled
# on shutdown). In-process async only.
ServiceFactory = Callable[[], Awaitable[None]]

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
        # Long-lived async services (ctx.add_service). Zero-arg coroutine
        # factories the manager supervises over the app's lifespan — see
        # `ServiceSupervisor`.
        self.services: list[ServiceFactory] = []


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


class DerivedDp:
    """A handle to one derived DP, returned by `ctx.derived_dp(device_id, dp)`.

    A derived DP is a value the plugin computes and publishes on the device's
    `{type}=derived` event topic (a sibling of the bridge's active/passive/state
    segments, so it never overwrites a real snapshot and is cleared for free by
    the bridge's retain scavenger when the device is removed). `set` renders the
    topic and a byte-faithful payload via the bridge's own helpers; `clear`
    empties it (the rule-level cleanup path — for when a derived value should
    vanish while its device still exists). The plugin never touches bridge
    config or builds a topic string itself."""

    def __init__(
        self, client: BridgeClient, state: State, device_id: str, dp: str, retain: bool | None
    ) -> None:
        self._client = client
        self._state = state
        self._device_id = device_id
        self._dp = dp
        self._retain = retain

    def _resolve_retain(self) -> bool:
        """`retain=None` mirrors the bridge's own `mqtt_retain`; an explicit
        value overrides it."""
        if self._retain is not None:
            return self._retain
        cfg = self._state.bridge_config_raw or {}
        return bool(cfg.get("mqtt_retain", False))

    async def set(self, value: Any) -> None:
        await self._client.publish_derived_dp(
            self._device_id, self._dp, value, retain=self._resolve_retain()
        )

    async def clear(self) -> None:
        await self._client.clear_derived_dp(self._device_id, self._dp)


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
        data_root: str | Path | None = None,
    ) -> None:
        self._registry = registry
        self._state = state
        self.api_version = PLUGIN_API_VERSION
        self.bridge_client = bridge_client
        # Root for plugin-owned persistent data dirs (sibling of the managed
        # plugin dir, so it survives plugin reinstalls). None → the process CWD,
        # which preserves the historical "./<name>" behaviour for unmanaged runs.
        self._data_root = Path(data_root) if data_root is not None else None

    def devices(self) -> dict[str, dict[str, Any]]:
        """Read-only snapshot of the cloud devices as `{id: raw_data}`.

        `raw_data` is the original per-device dict the manager loaded from the
        cloud JSON (the same shape a plugin would feed to a discovery
        generator). Returns a fresh outer dict each call so a plugin can't
        mutate the manager's device map; the inner `raw_data` dicts are shared
        by reference (not deep-copied — they can be large and plugins are
        expected to read, not write). Empty until the cloud snapshot loads."""
        return {did: dev.raw_data for did, dev in self._state.cloud.items()}

    def current_dps(self, device_id: str | None = None) -> dict[str, Any]:
        """Read-only snapshot of the current decoded DP values.

        With `device_id=None`, returns `{device_id: {dp: value}}` for every
        device the manager holds DPs for; with a `device_id`, returns just that
        device's `{dp: value}` (an empty dict if it's unknown). This is the same
        live state the DP watchers receive *deltas* of — call it at register /
        service-start time to seed a combinator from the values already on hand
        (e.g. ingested from the retained snapshot) instead of waiting for each
        source DP's next change.

        Fresh dicts are returned so a plugin can't mutate the manager's DP map;
        the DP values themselves are shared by reference (read, don't mutate)."""
        if device_id is not None:
            return dict(self._state.dps.get(device_id, {}))
        return {did: dict(dps) for did, dps in self._state.dps.items()}

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

    # ── reactive DP bus (api_version >= 2) ───────────────────────────────
    def watch_dps(self, handler: DpWatcher) -> None:
        """Fire `handler(device_id, dps, origin)` on every real device event.

        `dps` is the decoded `{dp: value}` delta; `origin` is `"retained"` for
        the retained-snapshot replay on (re)connect and `"device"` for a live
        event (so a watcher can distinguish an initial-state seed from a real
        change). Runs in-process (function call, no MQTT round-trip) from the
        manager's route path after state is updated. The handler keeps its own
        state in a closure, so accumulators / combinators are free. Use
        `current_dps()` to seed from values already on hand. Derived echoes
        never fire watchers."""
        self.bridge_client.add_dp_watcher(None, None, handler)

    def watch_device(self, device_id: str, handler: DpWatcher) -> None:
        """Like `watch_dps` but only for events from `device_id`."""
        self.bridge_client.add_dp_watcher(device_id, None, handler)

    def watch_dp(self, device_id: str, dp: str, handler: DpWatcher) -> None:
        """Like `watch_dps` but only when `device_id`'s event carries `dp`."""
        self.bridge_client.add_dp_watcher(device_id, str(dp), handler)

    def derived_dp(self, device_id: str, dp: str, *, retain: bool | None = None) -> DerivedDp:
        """A handle for publishing a derived DP on `device_id`'s
        `{type}=derived` topic. `retain=None` mirrors the bridge's
        `mqtt_retain`. See `DerivedDp`."""
        return DerivedDp(self.bridge_client, self._state, device_id, str(dp), retain)

    async def set_device_dp(self, device_id: str, dp: str, value: Any) -> None:
        """Command a real device's DP (external → Tuya), via the bridge's
        `set` action. The same path the web UI uses."""
        await self.bridge_client.set_device_dp(device_id, str(dp), value)

    def add_service(self, coro_factory: ServiceFactory) -> None:
        """Register a long-lived in-process async daemon (api_version >= 2).

        `coro_factory` is a zero-arg callable returning the coroutine the
        manager supervises: started after bootstrap, restarted with crash
        backoff (rate-limited), and cancelled + awaited on shutdown so nothing
        is orphaned. The coroutine uses the same DP bus (`watch_dps` /
        `set_device_dp` / `derived_dp`).

        In-process async ONLY — for blocking work use `asyncio.to_thread`
        inside the coroutine. The manager re-execs on plugin/config changes, so
        a service restarts cleanly there (state that must persist → write it to
        disk). A service whose external peers cannot tolerate that restart
        belongs in an *independent* process talking MQTT, not here."""
        self._registry.services.append(coro_factory)

    def state_namespace(self, name: str) -> StateNamespace:
        return StateNamespace(self._state, name)

    def data_dir(self, name: str) -> Path:
        """A persistent, user-visible directory a plugin owns for its on-disk data
        (created on demand, returned as a Path).

        Rooted at the data root (the parent of the managed plugin dir — i.e. a
        sibling of `plugins/`, next to the cloud file), NOT the process CWD, so
        the location is the same no matter where the manager was launched from and
        survives plugin reinstalls (it lives outside `plugins/`). `name` is a
        single path segment — traversal and absolute paths are refused."""
        seg = Path(name)
        if name != seg.name or not seg.name or seg.name in (".", ".."):
            raise ValueError(f"data_dir name must be a single path segment: {name!r}")
        base = self._data_root if self._data_root is not None else Path.cwd()
        target = base / seg.name
        target.mkdir(parents=True, exist_ok=True)
        return target

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


class ServiceSupervisor:
    """Supervises plugin services (`ctx.add_service`) over the app's lifespan.

    Each service is a long-lived coroutine. `start()` spawns one supervised
    task per service; a crash is logged and the service is respawned after a
    backoff, rate-limited so a crash-looping service can't spin. A clean return
    means the service finished — it is not respawned. `stop()` cancels and
    awaits every task, so none is orphaned (invariant: no orphan service). The
    shape mirrors the embedded-bridge supervisor; it is driven by the web app's
    lifespan (start on startup, stop on shutdown). Services thus restart with
    the manager on a re-exec — the same contract as the embedded bridge.
    """

    _CRASH_BACKOFF_SEC = 5.0
    _MAX_RESTARTS_IN_WINDOW = 5
    _WINDOW_SEC = 30.0

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Spawn a supervised task per registered service. Call once per
        lifespan (paired with `stop()`)."""
        for idx, factory in enumerate(self._registry.services):
            self._tasks.append(asyncio.create_task(self._supervise(idx, factory)))
        if self._tasks:
            logger.info("started %d plugin service(s)", len(self._tasks))

    async def _supervise(self, idx: int, factory: ServiceFactory) -> None:
        exits: list[float] = []
        while not self._stop.is_set():
            try:
                await factory()
            except asyncio.CancelledError:
                raise  # shutdown — let the task end as cancelled
            except Exception:  # noqa: BLE001 - any failure flows through respawn
                logger.exception("plugin service #%d crashed", idx)
            else:
                logger.info("plugin service #%d returned cleanly; not respawning", idx)
                return
            if self._stop.is_set():
                return
            now = time.monotonic()
            exits = [t for t in exits if now - t < self._WINDOW_SEC]
            exits.append(now)
            if len(exits) > self._MAX_RESTARTS_IN_WINDOW:
                logger.error(
                    "plugin service #%d crashed %d times in %.0fs — giving up",
                    idx,
                    len(exits),
                    self._WINDOW_SEC,
                )
                return
            logger.warning("plugin service #%d will respawn in %.1fs", idx, self._CRASH_BACKOFF_SEC)
            # Resolve early if stop() is signalled during the backoff.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._CRASH_BACKOFF_SEC)
                return
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """Cancel and await every service task. Idempotent."""
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


def _discover_dir_plugins(
    plugin_dirs: list[str],
    skip_packages: frozenset[str] = frozenset(),
) -> list[Callable[[PluginContext], None]]:
    """Import `register` callables from plugin packages/modules dropped into
    `plugin_dirs` (e.g. a mounted Docker `/data/plugins`) — no pip install
    needed.

    Each immediate child of a dir is loaded if it's a package (has
    `__init__.py`) or a top-level `*.py` file (names starting with `.`/`_` are
    skipped). The dir is put on `sys.path` and the child imported by name, so a
    plugin's `Path(__file__).parent / "static"` resolves to its real on-disk
    location and `add_page`/`add_header_init` static serving works unchanged.

    Per-item failures are logged and skipped — a broken folder never aborts the
    rest. Caveats (documented for users): a dir plugin can't pip-install
    dependencies (it gets stdlib + what `ctx`/the manager provide), and its
    package/module name must be distinctive enough not to shadow an installed
    module on `sys.path`."""
    found: list[Callable[[PluginContext], None]] = []
    for raw in plugin_dirs:
        root = Path(raw).expanduser()
        if not root.is_dir():
            logger.warning("plugin dir %s is not a directory; skipping", root)
            continue
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        for child in sorted(root.iterdir()):
            name = child.name
            if name.startswith((".", "_")):
                continue
            if child.is_dir():
                if not (child / "__init__.py").is_file():
                    continue
                mod_name = name
            elif child.suffix == ".py":
                mod_name = child.stem
            else:
                continue
            if mod_name in skip_packages:
                # Disabled via the install ledger — present on disk but not loaded.
                logger.info("skipping disabled dir plugin %r in %s", mod_name, root)
                continue
            try:
                module = importlib.import_module(mod_name)
                reg = getattr(module, "register", None)
                if callable(reg):
                    found.append(reg)
                    logger.info("loaded dir plugin %r from %s", mod_name, root)
                else:
                    logger.warning(
                        "dir plugin %r in %s has no register(ctx); skipping", mod_name, root
                    )
            except Exception:  # noqa: BLE001 - a broken dir plugin must not abort the rest
                logger.exception("failed to import dir plugin %r from %s; skipping", mod_name, root)
    return found


def discover_plugins(
    *,
    register_callables: list[Callable[[PluginContext], None]] | None = None,
    plugin_dirs: list[str] | None = None,
    skip_packages: frozenset[str] = frozenset(),
) -> list[Callable[[PluginContext], None]]:
    """Return all `register(ctx)` callables from the three sources, without
    calling them. Split out from `load_plugins` so a runtime rescan can diff the
    result against what's already applied and register only the new ones.

    Sources: `register_callables` (injected directly), the
    `rustuya_manager.plugins` entry-point group, and `plugin_dirs`
    (dropped-in packages/modules). For already-imported entry-point/dir plugins
    the SAME callable object comes back across calls (modules are cached in
    `sys.modules`), so caller-side identity dedup is reliable; a newly-dropped
    dir plugin imports fresh and yields a new callable."""
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

    if plugin_dirs:
        registers.extend(_discover_dir_plugins(plugin_dirs, skip_packages))

    return registers


def load_plugins(
    ctx: PluginContext,
    *,
    register_callables: list[Callable[[PluginContext], None]] | None = None,
    plugin_dirs: list[str] | None = None,
) -> None:
    """Discover and register all plugins into `ctx`.

    Three sources, all running through the same per-plugin isolation:
      - `register_callables`: injected directly (tests, or an explicit
        `build_app(..., plugins=[...])`).
      - the `rustuya_manager.plugins` entry-point group (pip-installed plugins).
      - `plugin_dirs`: packages/modules dropped into a directory (e.g. a mounted
        `/data/plugins`), via `_discover_dir_plugins`.

    Discovery, loading, and each `register()` call are individually guarded so
    one bad plugin can never take the manager down.
    """
    for reg in discover_plugins(register_callables=register_callables, plugin_dirs=plugin_dirs):
        try:
            reg(ctx)
        except Exception:  # noqa: BLE001 - register() failure is isolated per plugin
            logger.exception("plugin register(ctx) raised; skipping that plugin")
