"""Async MQTT client that talks to a running rustuya-bridge.

Built on aiomqtt's canonical reconnect-loop pattern: a single
`async with aiomqtt.Client(...)` lives inside a `while True:` so any
broker-side disconnect raises `MqttError` and we re-enter the context
after exponential backoff. Subscriptions are replayed from a cached
wildcard list on every (re)connect, so the manager survives broker
restarts without dropping the bridge's templated topic set.

Bootstrap order (mirrors the bridge's contract):
  1. Connect to the broker.
  2. Subscribe to `{root}/bridge/config` (retained). The bridge publishes
     its resolved config there at startup, so this is the source of truth
     for the user's customised topic/payload templates.
  3. Once the retained config arrives, derive the post-`{root}` templates
     and subscribe to event/message/scanner wildcards.
  4. Publish a `status` command and wait for the bridge's reply to
     populate the initial device list.

Lifecycle (used as an async context manager):
    async with BridgeClient(broker, root, state) as client:
        await client.wait_bootstrap(timeout=6.0)
        await client.publish_command("status", target_id="bridge")
        # ...web server runs...
    # exit -> reconnect task cancelled, aiomqtt context closed cleanly
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import aiomqtt
import pyrustuyabridge as pb

from .models import Device
from .plugins import topic_matches
from .state import BridgeTemplates, State

# Reverse payload parsing lives in the bridge crate (`rustuyabridge::payload`)
# and is re-exposed through pyrustuyabridge so the manager's seed-phase logic
# stays byte-identical to the bridge's own retained-snapshot reader. The
# algorithm was previously duplicated here as rustuya_manager.payload; the
# bridge author tagged that copy as canonical-to-delete in
# rustuya-bridge c7264aa (0.2.0rc8).

logger = logging.getLogger(__name__)

BRIDGE_CONFIG_TOPIC_TPL = "{root}/bridge/config"
BOOTSTRAP_TIMEOUT_SEC = 5.0

# Wrapper/envelope keys the bridge's error_helper always emits — they belong
# to the error frame itself, not the per-error detail. Anything else in the
# payload is treated as structured detail and surfaced after errorMsg.
_ERROR_ENVELOPE_KEYS = frozenset(
    {"errorCode", "errorMsg", "payloadStr", "errorPayloadObj", "payloadRaw"}
)


def _format_error_message(parsed: dict[str, Any]) -> str:
    """Render a bridge error payload as a single human-readable line.

    The bridge ships every error as `{errorCode, errorMsg, ...}` where the
    `...` is whatever structured context the device task chose to attach —
    e.g. `{reason: "ip_mismatch", configured, discovered}` for fixed-IP
    devices whose scanner sighting drifted. We append those extras to the
    base `errorMsg` so the UI surfaces them without per-code branching: any
    future error variant gets formatted the same way as soon as the bridge
    starts emitting it. Scalar fields only — nested dicts/lists would blow
    the single-line MSG cell out.
    """
    base = parsed.get("errorMsg") or parsed.get("payloadStr") or ""
    extras = {
        k: v
        for k, v in parsed.items()
        if k not in _ERROR_ENVELOPE_KEYS and v is not None and not isinstance(v, (dict, list))
    }
    if not extras:
        return str(base)
    details = ", ".join(f"{k}={v}" for k, v in extras.items())
    return f"{base} ({details})" if base else details


class _BrokerEndpoint(NamedTuple):
    host: str
    port: int
    tls: bool
    username: str | None
    password: str | None


# Schemes that select a TLS transport. `mqtts` is the conventional one; the
# others are accepted as aliases so a user's existing broker URL just works.
_TLS_SCHEMES = frozenset({"mqtts", "ssl", "tls", "mqtt+ssl"})


def _parse_broker_url(broker: str) -> _BrokerEndpoint:
    """Parse a broker URL into host / port / tls and any inline credentials.

    Accepts 'mqtt(s)://[user[:pass]@]host[:port]', 'host:port', or 'host'. A
    TLS scheme (mqtts/ssl/tls) flips `tls` and defaults the port to 8883;
    plaintext defaults to 1883. Inline `user:pass@` credentials are captured
    here; the BridgeClient prefers explicit username/password (CLI flag / env)
    over these, falling back to them so `mqtts://u:p@host` also works."""
    tls = False
    username: str | None = None
    password: str | None = None
    if "://" in broker:
        scheme, broker = broker.split("://", 1)
        tls = scheme.lower() in _TLS_SCHEMES
    if "@" in broker:
        creds, broker = broker.split("@", 1)
        user, sep, pw = creds.partition(":")
        username = user or None
        password = pw if sep else None
    if ":" in broker:
        host, port_s = broker.rsplit(":", 1)
        port = int(port_s)
    else:
        host = broker
        port = 8883 if tls else 1883
    return _BrokerEndpoint(host, port, tls, username, password)


class BridgeClient:
    """Async-context-managed MQTT client.

    Usage:
        async with BridgeClient(broker, root, state) as client:
            await client.wait_bootstrap(timeout=6.0)
            await client.publish_command("status", target_id="bridge")
            ...

    Public surface:
        await client.wait_bootstrap(timeout=...)   # block until templates resolved
        await client.publish_command(action, ...)  # forward-render + publish
    """

    # Exponential backoff bounds for the (re)connect loop. Exposed as class
    # attributes so tests can override them to 0 for fast suites.
    _INITIAL_BACKOFF_SEC: float = 1.0
    _MAX_BACKOFF_SEC: float = 60.0
    # Cap the internal aiomqtt incoming queue so a wedged dispatch surfaces as
    # a paho-side warning rather than unbounded memory growth. 1000 is well
    # above any realistic manager-side burst (bridge `status` reply is the
    # biggest single payload and arrives once per request).
    _MAX_QUEUED_INCOMING: int = 1000

    def __init__(
        self,
        broker: str,
        root: str,
        state: State,
        *,
        client_id: str = "rustuya-manager",
        on_event: Callable[[str, dict[str, str], Any, dict[str, Any] | None], Awaitable[None]]
        | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        ep = _parse_broker_url(broker)
        self.host = ep.host
        self.port = ep.port
        self.tls = ep.tls
        # Explicit creds (CLI flag / env) win over any embedded in the URL;
        # fall back to the inline ones so `mqtts://user:pass@host` also works.
        self.username = username if username is not None else ep.username
        self.password = password if password is not None else ep.password
        self.root = root
        self.state = state
        self._client_id = client_id
        self._on_event = on_event

        # Set inside the reconnect loop while the aiomqtt context is alive;
        # cleared on disconnect. publish_command() refuses when not set.
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._bootstrap_done = asyncio.Event()
        self._reconnect_task: asyncio.Task[None] | None = None
        # Cache of the runtime wildcards we want to keep subscribed. Updated
        # whenever templates resolve and replayed by `_subscribe_initial` on
        # every (re)connect so subscriptions survive broker hiccups even with
        # aiomqtt's default clean session.
        self._runtime_wildcards: list[str] = []
        # Queues subscribed to scanner-topic sightings. Callers register via
        # `subscribe_scanner()` (and must unsubscribe in a `finally`) to
        # receive every sighting the bridge publishes plus the bridge's
        # empty-dict scan-end marker. The `LanScanCoordinator` in scan.py
        # is currently the only subscriber, but the list shape is preserved
        # so a future debug/CLI tap could attach without disturbing it.
        self._scanner_subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        # Plugin-registered MQTT taps: (topic_filter, async handler). Populated
        # via `add_plugin_subscription` (driven by the plugin host). Each filter
        # is (re)subscribed on every connect by `_subscribe_initial`, and
        # `_dispatch` routes any matching message to the handler — independent
        # of the bridge's own templated topics. Generalises the scanner-tap
        # pattern above to arbitrary HA-agnostic subscribers.
        self._plugin_subscriptions: list[
            tuple[str, Callable[[str, str, bool], Awaitable[None]]]
        ] = []
        # Plugin-registered DP watchers (plugin runtime, reactive pillar):
        # (device_id_filter, dp_filter, handler). A None filter means "any".
        # Fired from `_route` after `merge_dps` for a real device event —
        # in-process function calls, no MQTT round-trip. `add_dp_watcher`
        # appends; `_dispatch_dp_watchers` fans out with per-handler isolation.
        self._dp_watchers: list[
            tuple[str | None, str | None, Callable[[str, dict[str, Any], str], Awaitable[None]]]
        ] = []
        # In-progress buffer for a paginated `status` reply (see
        # `_handle_status_page`). None when no page-through is active; a partial
        # {id: Device} map while one is. Committed to state on the final page.
        self._status_accum: dict[str, Device] | None = None

    def _client_kwargs(self) -> dict[str, Any]:
        """Build the aiomqtt.Client constructor kwargs.

        TLS and auth keys are added only when configured, so a plaintext,
        unauthenticated connection is constructed exactly as before (no
        behavioural change for the default `mqtt://` + no-creds case).
        `aiomqtt.TLSParameters()` with no overrides uses the platform's default
        trust store — validating against public-CA brokers, matching the
        bridge's own native-root-cert TLS handling."""
        kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "identifier": self._client_id,
            "max_queued_incoming_messages": self._MAX_QUEUED_INCOMING,
        }
        if self.username is not None:
            kwargs["username"] = self.username
        if self.password is not None:
            kwargs["password"] = self.password
        if self.tls:
            kwargs["tls_params"] = aiomqtt.TLSParameters()
        return kwargs

    # ── async context manager ────────────────────────────────────────────
    async def __aenter__(self) -> BridgeClient:
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        self._connected.clear()
        self._client = None

    async def wait_bootstrap(self, timeout: float | None = None) -> None:
        """Block until the first retained bridge/config has been processed
        (or the internal timeout guard has applied fallback templates).

        On timeout we silently return — the caller can inspect
        `state.warnings` to distinguish "broker still unreachable" from
        "bridge offline, using defaults"."""
        if timeout is None:
            await self._bootstrap_done.wait()
        else:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._bootstrap_done.wait(), timeout)

    # ── reconnect loop ───────────────────────────────────────────────────
    async def _reconnect_loop(self) -> None:
        """Owns the aiomqtt client. Runs for the lifetime of __aenter__.

        Each loop iteration enters a fresh `aiomqtt.Client` context — that
        is the canonical aiomqtt pattern because any broker-side disconnect
        raises `MqttError` out of `async for messages`. The cached
        `_runtime_wildcards` are re-subscribed on every reconnect, and after
        the first successful bootstrap we additionally re-issue a `status`
        request so the device list reflects any state change that happened
        during the disconnect gap (clean-session means we lose live events
        published while we were away)."""
        delay = self._INITIAL_BACKOFF_SEC
        was_disconnected_after_bootstrap = False
        bootstrap_guard: asyncio.Task[None] | None = None
        try:
            while True:
                try:
                    async with aiomqtt.Client(**self._client_kwargs()) as client:
                        if delay != self._INITIAL_BACKOFF_SEC:
                            logger.info("MQTT reconnected; resetting backoff.")
                            delay = self._INITIAL_BACKOFF_SEC
                        await self.state.clear_warning("broker_unreachable")
                        self._client = client
                        await self._subscribe_initial(client)
                        self._connected.set()
                        # On a *re*connect after bootstrap, re-request status so
                        # the device list reflects whatever changed during the
                        # gap. First-connect path triggers status from
                        # `_on_bridge_config` after templates resolve, so don't
                        # double-fire here.
                        if was_disconnected_after_bootstrap and self._bootstrap_done.is_set():
                            asyncio.create_task(self.publish_command("status", target_id="bridge"))
                            was_disconnected_after_bootstrap = False
                        # First-time bootstrap fallback — if bridge/config never
                        # arrives, apply defaults so the UI isn't frozen waiting
                        # for templates forever.
                        if bootstrap_guard is None and not self._bootstrap_done.is_set():
                            bootstrap_guard = asyncio.create_task(self._bootstrap_timeout_guard())
                        async for msg in client.messages:
                            try:
                                await self._dispatch(
                                    str(msg.topic),
                                    msg.payload.decode("utf-8", "replace")
                                    if isinstance(msg.payload, bytes)
                                    else str(msg.payload),
                                    retain=bool(msg.retain),
                                )
                            except Exception:  # noqa: BLE001 - log + keep running
                                logger.exception("Failed to handle MQTT message on %s", msg.topic)
                except aiomqtt.MqttError as e:
                    self._connected.clear()
                    self._client = None
                    if self._bootstrap_done.is_set():
                        was_disconnected_after_bootstrap = True
                    await self.state.set_warning(
                        "broker_unreachable",
                        "error",
                        f"MQTT broker {self.host}:{self.port} unreachable ({e}); "
                        f"retrying every {int(delay)}s.",
                    )
                    logger.warning("MQTT error: %s — reconnect in %.1fs", e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._MAX_BACKOFF_SEC)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    # Unlike tuya2mqtt (which terminates and relies on systemd
                    # to restart), the manager runs without a supervisor — log
                    # the traceback and reconnect so a single bug doesn't take
                    # the whole UI down.
                    self._connected.clear()
                    self._client = None
                    if self._bootstrap_done.is_set():
                        was_disconnected_after_bootstrap = True
                    logger.exception("Unexpected MQTT worker crash; will reconnect")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._MAX_BACKOFF_SEC)
        finally:
            self._connected.clear()
            self._client = None
            if bootstrap_guard is not None and not bootstrap_guard.done():
                bootstrap_guard.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await bootstrap_guard

    async def _subscribe_initial(self, client: aiomqtt.Client) -> None:
        """Replay subscriptions on every (re)connect.

        On first connect `_runtime_wildcards` is empty, so we only subscribe
        to the bridge config topic — the retained config triggers
        `_on_bridge_config` which then populates the wildcard cache and
        issues the runtime subscribes. On reconnect the cache is non-empty
        and we re-subscribe to both bridge/config (idempotent) and every
        runtime wildcard so live events resume immediately."""
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", self.root)
        await client.subscribe(cfg_topic)
        logger.info("Subscribed to bridge config: %s", cfg_topic)
        for wildcard in self._runtime_wildcards:
            await client.subscribe(wildcard)
            logger.info("Re-subscribed: %s", wildcard)
        # Replay plugin taps too so their (often retained) topic flows — e.g.
        # `homeassistant/#` — resume on every (re)connect.
        for topic_filter, _ in self._plugin_subscriptions:
            await client.subscribe(topic_filter)
            logger.info("Re-subscribed plugin filter: %s", topic_filter)

    async def _bootstrap_timeout_guard(self) -> None:
        """Apply default templates if the retained bridge/config doesn't
        arrive within BOOTSTRAP_TIMEOUT_SEC. Without this the UI sits empty
        forever when the bridge is offline."""
        try:
            await asyncio.wait_for(self._bootstrap_done.wait(), BOOTSTRAP_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout waiting for %s retained config — bridge may be offline. "
                "Falling back to bridge defaults.",
                BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", self.root),
            )
            await self._apply_default_templates()

    # ── dispatch ─────────────────────────────────────────────────────────
    async def _dispatch(self, topic: str, payload: str, *, retain: bool = False) -> None:
        # The retained bridge/config arrives first; once it does we resolve
        # templates and subscribe to event/message/scanner.
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", self.root)
        if topic == cfg_topic:
            if payload == "":
                logger.warning("Bridge config cleared (bridge offline?); ignoring")
                return
            await self._on_bridge_config(payload)
            return

        # Plugin taps run before the bridge-template guard so plugin topics are
        # routed regardless of bootstrap state (and so retained plugin topics
        # delivered at subscribe-time reach their handler immediately).
        await self._dispatch_plugins(topic, payload, retain)

        if self.state.templates is None:
            logger.debug("Dropping %s — templates not resolved yet", topic)
            return

        tpls = self.state.templates

        # Try event topic first (most frequent), then message topic, then scanner
        vars_ = pb.match_topic(topic, tpls.event)
        matched_as = "event"
        if vars_ is None:
            vars_ = pb.match_topic(topic, tpls.message)
            matched_as = "message"
        if vars_ is None:
            vars_ = pb.match_topic(topic, tpls.scanner)
            matched_as = "scanner"
        if vars_ is None:
            logger.debug("Unmatched topic %s", topic)
            return

        # Empty payload on a topic typically signals retain-clearing — skip.
        if payload == "":
            logger.debug("Skipping empty payload (retain-clear) on %s", topic)
            return

        try:
            parsed = pb.parse_payload(payload, vars_)
        except Exception as e:  # noqa: BLE001
            logger.warning("parse_payload failed for %s: %s", topic, e)
            return

        await self._route(matched_as, vars_, parsed, payload, retain=retain)

    def _resolve_device_key(self, vars_: dict[str, str], parsed: dict[str, Any]) -> str | None:
        """Find the device's bridge ID.

        Order: topic-extracted `id` → payload-merged `id` → reverse-lookup by
        topic/payload `name` in current bridge state. Returns None when none
        of these resolves — the caller skips the update rather than creating
        a phantom DPS entry under an unresolved key."""
        did = vars_.get("id") or parsed.get("id")
        if did:
            return str(did)
        name = vars_.get("name") or parsed.get("name")
        if not name:
            return None
        for dev in self.state.bridge.values():
            if dev.name == name:
                return dev.id
        logger.debug(
            "Cannot resolve device id for event (vars=%s, parsed has id=%s, name=%s); skipping",
            vars_,
            parsed.get("id"),
            name,
        )
        return None

    async def _route(
        self,
        matched_as: str,
        vars_: dict[str, str],
        parsed: Any,
        payload_str: str = "",
        *,
        retain: bool = False,
    ) -> None:
        """Updates State based on what kind of message arrived."""
        extras: dict[str, Any] = {"retain": retain}
        if matched_as == "message":
            # Response or error reply. The bridge's `status` action has the
            # full device list in `devices`; record it.
            if isinstance(parsed, dict):
                target = vars_.get("id", "bridge")
                level = vars_.get("level", "")
                if parsed.get("action") == "status" and isinstance(parsed.get("devices"), dict):
                    await self._handle_status_page(parsed)
                # The bridge publishes per-device connection state under the
                # `error` level: errorCode=0 means "Connection Successful",
                # any non-zero code means the device is unreachable / errored.
                if level == "error" and target != "bridge" and "errorCode" in parsed:
                    code = parsed.get("errorCode")
                    msg = _format_error_message(parsed)
                    online = code == 0
                    await self.state.set_live_status(
                        target,
                        "online" if online else "offline",
                        code=code,
                        message=msg,
                    )
                # Reactive state updates after action-result responses. The
                # bridge republishes its retained `bridge/config` when devices
                # change, but that handler is idempotent on templates and
                # doesn't refresh device lists — we have to act on the action
                # response ourselves.
                action = parsed.get("action")
                status_val = parsed.get("status")
                if action == "remove" and status_val == "ok":
                    # Drop the device from every per-device bucket. The bridge
                    # has already cleared the retained MQTT data on its side,
                    # so anything we kept (DPS / live / last-seen) is stale.
                    rid = parsed.get("id") or target
                    if rid and rid != "bridge":
                        await self.state.remove_device(rid)
                    else:
                        await self.state.record_response(target, parsed, retained=retain)
                elif action == "add" and status_val == "ok":
                    # The add ack doesn't carry the device fields the bridge
                    # ended up storing; ask for a status refresh so state.bridge
                    # picks up the new/updated entry authoritatively.
                    await self.state.record_response(target, parsed, retained=retain)
                    asyncio.create_task(self.publish_command("status", target_id="bridge"))
                elif (
                    action == "clear"
                    and status_val == "ok"
                    and (target == "all" or parsed.get("id") == "all")
                ):
                    # Bridge has wiped its entire device list; mirror locally
                    # so the UI doesn't keep ghost rows from devices that no
                    # longer exist on the bridge. The retained bridge/config
                    # republish would update templates but not the device
                    # list, so we must act on the action ack ourselves.
                    # The `id=="all"` guard keeps a malformed message from
                    # nuking the whole state — the bridge contract is
                    # `action=clear` ↔ `id="all"`, anything else is suspect.
                    await self.state.clear_all_devices()
                    await self.state.record_response(target, parsed, retained=retain)
                else:
                    await self.state.record_response(target, parsed, retained=retain)
        elif matched_as == "event":
            # The `derived` {type} segment is the manager's own derived-DP
            # republish (plugin runtime). The broker echoes it back on our
            # event subscription, but re-ingesting it as a device event would
            # stamp last_seen/liveness and could loop into a watcher — so drop
            # it entirely here. The bridge only ever emits active/passive/state,
            # so a `derived` type can only be our own echo. (Deployments whose
            # event template has no {type} can't carry a derived segment, so
            # `.get("type")` is None there and this guard is a no-op.)
            if vars_.get("type") == "derived":
                logger.debug("Dropping own derived echo for id=%s", vars_.get("id"))
                return
            if isinstance(parsed, dict):
                key = self._resolve_device_key(vars_, parsed)
                dps = await self._extract_dps_from_event(vars_, parsed, payload_str)
                if key and dps:
                    await self.state.merge_dps(key, dps, retained=retain)
                    # Data flowing means the device is alive — mark online.
                    # Leave message empty: the bridge's error-channel sends
                    # human-readable strings like "Connection Successful",
                    # and event-derived liveness has no equivalent to put
                    # in the MSG row. The online dot + edge color already
                    # convey state; MSG should only fire for real diagnostics.
                    await self.state.set_live_status(key, "online", code=0, message="")
                    # Fan out to plugin DP watchers. After merge_dps returns
                    # (so the lock is released and state reflects the update),
                    # never under it.
                    await self._dispatch_dp_watchers(key, dps)
                # Surface the resolved key+dps to listeners (CLI prints these).
                extras["device_id"] = key
                extras["dps"] = dps
        elif matched_as == "scanner":
            # Push every sighting (and the bridge's end-of-scan empty
            # payload) to every active scan_collect() subscriber. The
            # raw dict shape matches what tuyawizard.apply_scan_results
            # expects: {id, ip, version?, product_key?} per sighting,
            # {} as the terminal marker.
            if isinstance(parsed, dict):
                for q in self._scanner_subscribers:
                    q.put_nowait(parsed)

        if self._on_event is not None:
            await self._on_event(matched_as, vars_, parsed, extras)

    async def _handle_status_page(self, parsed: dict[str, Any]) -> None:
        """Accumulate a (possibly paginated) `status` reply; commit when complete.

        The bridge paginates `devices` to stay under broker packet limits: every
        reply carries `offset`/`returned`/`has_more` plus the authoritative
        `device_count` and `mqtt_drop_count`. We buffer pages keyed by id and
        replace `state.bridge` wholesale only on the final page, so a fleet
        larger than the bridge's page size (default 50) isn't truncated to the
        first page. Older bridges that don't paginate omit `has_more`, so a lone
        reply commits immediately — behaviour-identical to the pre-paging path.
        """
        devices = parsed["devices"]
        offset = parsed.get("offset", 0)
        returned = parsed.get("returned", len(devices))
        has_more = bool(parsed.get("has_more", False))

        # offset 0 starts a fresh snapshot (covers the unpaginated and first-page
        # cases). A mid-stream restart (another trigger re-issued status) simply
        # begins again; the id-keyed merge keeps the result convergent. A
        # non-zero offset arriving with no buffer (e.g. page 0 lost across a
        # reconnect) still opens one rather than dropping the data.
        if offset == 0 or self._status_accum is None:
            self._status_accum = {}
        for did, d in devices.items():
            self._status_accum[did] = Device.from_dict(d)

        if has_more and returned > 0:
            # Fetch the next window. Omit `limit` so we inherit the bridge's
            # default page size — the packet budget the first page already
            # proved safe for this broker.
            await self.publish_command(
                "status", target_id="bridge", extra={"offset": offset + returned}
            )
            return

        # Final page (or unpaginated reply) — commit the assembled snapshot and
        # the bridge diagnostics in a single state bump.
        accum = self._status_accum
        self._status_accum = None
        drop_count = parsed.get("mqtt_drop_count", 0)
        await self.state.set_bridge(
            accum,
            device_count=parsed.get("device_count"),
            mqtt_drop_count=drop_count,
        )
        await self._surface_mqtt_drops(drop_count)

    async def _surface_mqtt_drops(self, drop_count: Any) -> None:
        """Raise (or clear) a UI warning for bridge-side MQTT publish drops.

        `mqtt_drop_count` is cumulative since the bridge started, so any non-zero
        value means at least one live update was lost to broker backpressure or a
        packet-size limit. Routed through the standard warning channel so it
        rides the existing banner + WS broadcast."""
        if isinstance(drop_count, int) and drop_count > 0:
            await self.state.set_warning(
                "mqtt_drops",
                "warning",
                f"Bridge dropped {drop_count} MQTT publish(es) — broker "
                f"backpressure or packet-size limit. Some live updates may be "
                f"missing (count is cumulative since the bridge started).",
            )
        else:
            await self.state.clear_warning("mqtt_drops")

    async def _extract_dps_from_event(
        self, vars_: dict[str, str], parsed: dict[str, Any], payload_str: str
    ) -> dict[str, Any] | None:
        """Returns a {dp: value} map extracted from an event payload, or None.

        Delegates to the bridge's own `parse_seed_dps` (exposed by
        pyrustuyabridge ≥ rc19) so the manager reads event payloads
        byte-identically to how the bridge writes them — both topic modes:

          - Single-DP (`{dp}` in the event topic): the captured value is that
            DP's value; wrapped into {dp: value}.
          - Multi-DP (no `{dp}`): the payload is the full DPS object — e.g. a
            `get` reply on `event/passive/{id}` carrying `{"1":true,...}`.
            Returned as-is.

        Handles the default `{value}` template and arbitrary JSON-shaped
        templates ({value}, {dps}, wrapped, etc.). Returns None when the
        user's template isn't a readable shape (a warning is already raised at
        bootstrap to nudge them to fix it)."""
        tpls = self.state.templates
        if tpls and payload_str:
            dps = pb.parse_seed_dps(payload_str, vars_.get("dp"), tpls.payload)
            if isinstance(dps, dict) and dps:
                return dps

        # Fall back to whatever the bridge's parse_payload produced.
        dps = parsed.get("dps")
        if isinstance(dps, dict) and dps:
            return dps
        return None

    # ── bridge-config handling ──────────────────────────────────────────
    async def _on_bridge_config(self, payload: str) -> None:
        # Empty/blank payload = retained message was cleared. Bridge's
        # `reconfigure` action clears its own retained on exit, and the LWT
        # clears it on ungraceful death — both look identical to subscribers
        # (retain=True, payload=""). Pre-bootstrap empties are handled by
        # `bridge_offline` (default-templates fallback timeout); post-bootstrap,
        # surface a persistent banner so a user who changed `mqtt_root_topic`
        # realises the manager is still subscribed to the old root and won't
        # see the new config at `<new-root>/bridge/config`.
        if not payload.strip():
            if self._bootstrap_done.is_set():
                await self.state.set_warning(
                    "bridge_config_cleared",
                    "warning",
                    "Bridge config was cleared (reconfigure or bridge offline). "
                    "If you changed mqtt_root_topic, the manager is still "
                    "subscribed to the old root — restart with the new "
                    "--mqtt-root-topic.",
                )
            return

        try:
            cfg = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in bridge/config: %s", e)
            return

        # Bridge may publish its own root; honor it.
        root = cfg.get("mqtt_root_topic") or self.root

        # Substitute {root} once — same step the bridge itself performs.
        def resolve(key: str, default: str = "") -> str:
            tpl = cfg.get(key) or default
            return pb.render_template(tpl, {"root": root})

        templates = BridgeTemplates(
            root=root,
            command=resolve("mqtt_command_topic", "{root}/command"),
            event=resolve("mqtt_event_topic", "{root}/event/{type}/{id}"),
            message=resolve("mqtt_message_topic", "{root}/{level}/{id}"),
            scanner=resolve("mqtt_scanner_topic", "{root}/scanner"),
            payload=cfg.get("mqtt_payload_template") or "{value}",
        )

        # A valid config arrived → both "no config" warnings can come down.
        # Clear before the idempotence check below so a same-templates
        # re-delivery (the post-reconfigure / post-LWT republish on the
        # unchanged root) still clears `bridge_config_cleared` that was set
        # during the gap. `bridge_offline` is only set by the default-
        # templates fallback so it's a no-op here in the steady state, but
        # clearing it unconditionally keeps the two warnings symmetrical.
        await self.state.clear_warning("bridge_offline")
        await self.state.clear_warning("bridge_config_cleared")

        # Idempotence check: the retained bridge/config message can be
        # re-delivered every time we subscribe to a wildcard that also matches
        # it (e.g. message_topic="{root}/{level}/{id}" → wildcard "{root}/+/+"
        # which matches "{root}/bridge/config" too). Without this guard, each
        # re-delivery triggers another re-subscribe → another re-delivery →
        # infinite bootstrap loop.
        if self.state.templates == templates and self._bootstrap_done.is_set():
            logger.debug("bridge/config re-delivered, templates unchanged — skipping")
            return

        self.root = root
        await self.state.set_templates(templates)
        # Keep the raw config dict around for plugins (read-only via
        # PluginContext.bridge_config). Stored after the idempotence check so
        # we only update it when the templates actually change.
        await self.state.set_bridge_config_raw(cfg)
        await self._subscribe_runtime_topics(templates)
        await self._validate_payload_template(templates.payload)
        if not self._bootstrap_done.is_set():
            await self._request_initial_status()
            self._bootstrap_done.set()
            logger.info("Bootstrap complete — templates resolved for root %r", root)
        else:
            logger.info("Bridge config updated — templates re-resolved for root %r", root)

    async def _validate_payload_template(self, template: str | None) -> None:
        """Surface a UI warning when the bridge's payload template isn't a
        shape the manager can extract from. The fix is at the bridge config
        level (mqtt_payload_template), not in the manager.

        The actual structural validation lives in the bridge's
        validate_payload_template binding — only the "no template at all"
        case is special-cased here, since the binding takes &str and can't
        represent None directly."""
        if not template:
            ok, message = False, "No payload template received from bridge."
        else:
            ok, message = pb.validate_payload_template(template)
        if ok:
            await self.state.clear_warning("payload_template")
        else:
            logger.warning("Payload template not parseable: %s", message)
            await self.state.set_warning("payload_template", "warning", message)

    async def _apply_default_templates(self) -> None:
        """Fallback when bridge/config didn't arrive (bridge offline)."""

        def resolve(tpl: str) -> str:
            return pb.render_template(tpl, {"root": self.root})

        templates = BridgeTemplates(
            root=self.root,
            command=resolve("{root}/command"),
            event=resolve("{root}/event/{type}/{id}"),
            message=resolve("{root}/{level}/{id}"),
            scanner=resolve("{root}/scanner"),
            payload="{value}",
        )
        await self.state.set_templates(templates)
        await self._subscribe_runtime_topics(templates)
        await self._validate_payload_template(templates.payload)
        self._bootstrap_done.set()
        # Surface "we're up but the bridge isn't" so the UI doesn't pretend
        # everything is fine. Cleared in _on_bridge_config when a real
        # retained config eventually arrives.
        await self.state.set_warning(
            "bridge_offline",
            "warning",
            (
                f"No bridge config received on {BRIDGE_CONFIG_TOPIC_TPL.replace('{root}', self.root)} "
                f"within {BOOTSTRAP_TIMEOUT_SEC}s. Using default topic templates as a placeholder — "
                f"devices will appear once a bridge starts on root='{self.root}'."
            ),
        )

    async def _subscribe_runtime_topics(self, t: BridgeTemplates) -> None:
        """Diff the cached wildcards against the new template-derived set,
        then (un)subscribe accordingly.

        The cache is updated BEFORE issuing subscribe/unsubscribe so that, if
        an MqttError fires mid-way through (broker dropped during the call),
        the reconnect's `_subscribe_initial` replays the intended set rather
        than the partial one."""
        wildcards = [pb.tpl_to_wildcard(tpl, t.root) for tpl in (t.event, t.message, t.scanner)]
        # Deduplicate while preserving order — custom templates can collapse to
        # the same wildcard (e.g. event {root}/x/{id} and message {root}/x/{id}).
        seen: set[str] = set()
        new_wildcards = [w for w in wildcards if not (w in seen or seen.add(w))]

        stale = [w for w in self._runtime_wildcards if w not in new_wildcards]
        previously = set(self._runtime_wildcards)
        newcomers = [w for w in new_wildcards if w not in previously]

        # Intent-first cache update; see docstring.
        self._runtime_wildcards = new_wildcards

        if self._client is None:
            # Not currently connected — cache is enough; reconnect replays.
            return

        for w in stale:
            await self._client.unsubscribe(w)
            logger.info("Unsubscribed: %s", w)
        # Subscribe only the newcomers — re-subscribing an existing wildcard
        # forces the broker to re-deliver every retained message, which is
        # both wasteful and (in the case of bridge/config) the trigger for an
        # infinite bootstrap loop.
        for w in newcomers:
            await self._client.subscribe(w)
            logger.info("Subscribed: %s", w)

    async def _request_initial_status(self) -> None:
        await self.publish_command("status", target_id="bridge")

    # ── command publishing ──────────────────────────────────────────────
    async def publish_command(
        self,
        action: str,
        *,
        target_id: str | None = None,
        target_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Build a concrete command topic and publish a JSON payload.

        Any `{var}` in the bridge's command_topic template that has a matching
        kwarg in scope is substituted — so a template of `{root}/cmd/{id}/{action}`
        and call `publish_command("status", target_id="bridge")` yields the
        topic `{root_value}/cmd/bridge/status`. The full payload also includes
        these fields so the bridge accepts it regardless of where the data is
        carried.

        Raises `RuntimeError` when the broker is currently disconnected or the
        publish itself fails — FastAPI handlers translate this into a clear
        503 + UI toast rather than silently dropping the command."""
        if not self._connected.is_set() or self._client is None:
            raise RuntimeError("MQTT broker not connected — try again shortly")
        if self.state.templates is None:
            raise RuntimeError("templates not yet resolved (bootstrap incomplete)")

        vars_ = {"action": action}
        if target_id:
            vars_["id"] = target_id
        if target_name:
            vars_["name"] = target_name

        topic = pb.render_template(self.state.templates.command, vars_)

        payload: dict[str, Any] = {"action": action}
        if target_id:
            payload["id"] = target_id
        if target_name:
            payload["name"] = target_name
        if extra:
            payload.update(extra)

        body = json.dumps(payload)
        logger.debug("publish %s %s", topic, body)
        try:
            await self._client.publish(topic, body, qos=1)
        except aiomqtt.MqttError as e:
            raise RuntimeError(f"MQTT publish failed: {e}") from e

    async def publish_raw(
        self, topic: str, payload: str, *, retain: bool = False, qos: int = 1
    ) -> None:
        """Publish an arbitrary payload to an arbitrary topic.

        Unlike `publish_command` (which forward-renders the bridge's command
        template and always targets the bridge), this is a generic escape hatch
        for plugins that own topics outside the bridge's namespace — e.g.
        rustuya-ha writing/clearing retained `homeassistant/.../config`
        discovery messages. The caller supplies the fully-formed topic and
        payload; the host does not interpret either.

        `retain=True` is the common case for discovery/config topics (a clear
        is `publish_raw(topic, "", retain=True)`). Raises `RuntimeError` when
        the broker is disconnected or the publish fails, mirroring
        `publish_command` so plugin API handlers can surface a 503."""
        if not self._connected.is_set() or self._client is None:
            raise RuntimeError("MQTT broker not connected — try again shortly")
        try:
            await self._client.publish(topic, payload, qos=qos, retain=retain)
        except aiomqtt.MqttError as e:
            raise RuntimeError(f"MQTT publish failed: {e}") from e

    # ── plugin MQTT taps ─────────────────────────────────────────────────
    def add_plugin_subscription(
        self, topic_filter: str, handler: Callable[[str, str, bool], Awaitable[None]]
    ) -> None:
        """Register a plugin handler for `topic_filter`.

        Synchronous: the filter is cached for replay by `_subscribe_initial` on
        every (re)connect, and if a broker connection is already live the
        subscribe is issued immediately on a fire-and-forget task so a plugin
        registered after bootstrap starts receiving without waiting for the
        next reconnect."""
        self._plugin_subscriptions.append((topic_filter, handler))
        if self._client is None:
            return  # not connected yet — _subscribe_initial replays on connect
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (e.g. registered at build time in a test);
            # the cached filter is replayed by _subscribe_initial on connect
        client = self._client

        async def _subscribe_now() -> None:
            try:
                await client.subscribe(topic_filter)
                logger.info("Subscribed plugin filter: %s", topic_filter)
            except Exception:  # noqa: BLE001 - reconnect replay is the backstop
                logger.exception("immediate subscribe for plugin filter %s failed", topic_filter)

        loop.create_task(_subscribe_now())

    async def _dispatch_plugins(self, topic: str, payload: str, retain: bool) -> None:
        """Route a message to every plugin handler whose filter matches.

        Each handler is isolated: an exception is logged and the remaining
        handlers (and the bridge's own routing downstream) still run."""
        for topic_filter, handler in self._plugin_subscriptions:
            if topic_matches(topic_filter, topic):
                try:
                    await handler(topic, payload, retain)
                except Exception:  # noqa: BLE001 - one bad handler must not break others
                    logger.exception("plugin handler for %s raised on %s", topic_filter, topic)

    # ── plugin runtime: reactive DP bus ──────────────────────────────────
    def add_dp_watcher(
        self,
        device_id: str | None,
        dp: str | None,
        handler: Callable[[str, dict[str, Any], str], Awaitable[None]],
    ) -> None:
        """Register a DP watcher (plugin runtime, reactive pillar).

        `device_id`/`dp` are optional filters (None = any). The handler is
        `async (device_id, dps, origin)` and is called from `_route` for every
        real device event whose update matches. In-process function call — no
        MQTT round-trip."""
        self._dp_watchers.append((device_id, dp, handler))

    async def _dispatch_dp_watchers(self, device_id: str, dps: dict[str, Any]) -> None:
        """Fan a decoded device event out to matching DP watchers.

        Isolated per handler: an exception is logged and the rest still run.
        `origin` is `"device"` — derived echoes never reach here (dropped in
        `_route` on the `{type}=derived` segment)."""
        for did_filter, dp_filter, handler in self._dp_watchers:
            if did_filter is not None and did_filter != device_id:
                continue
            if dp_filter is not None and dp_filter not in dps:
                continue
            try:
                await handler(device_id, dps, "device")
            except Exception:  # noqa: BLE001 - one bad watcher must not break others
                logger.exception("dp watcher raised for %s", device_id)

    async def set_device_dp(self, device_id: str, dp: str, value: Any) -> None:
        """Outbound: command a real device's DP (external → Tuya).

        Thin wrapper over `publish_command("set", …)` — the bridge's `Set`
        action takes a `dps` map. Same path the web UI / an external controller
        uses."""
        await self.publish_command("set", target_id=device_id, extra={"dps": {str(dp): value}})

    def _render_derived(
        self, tpls: BridgeTemplates, device_id: str, dp: str, value: Any
    ) -> tuple[str, str]:
        """Render the `{type}=derived` topic + a byte-faithful payload for one DP.

        Reuses the bridge's own `render_template` (topic) and verifies the
        payload by round-tripping it through the bridge's own `parse_seed_dps`
        (payload) — so a consumer reading the derived topic with the bridge's
        payload template gets exactly `{dp: value}`. Raises if the deployment's
        event template lacks `{type}` (no room for a derived segment — it would
        collide with the device's real event topic) or if the value can't be
        rendered byte-faithfully under the configured payload template."""
        if "{type}" not in tpls.event:
            raise RuntimeError(
                "derived DPs require '{type}' in the event topic template; "
                f"{tpls.event!r} has no derived segment to publish into safely"
            )
        single = "{dp}" in tpls.event
        topic_vars = {"root": tpls.root, "type": "derived", "id": device_id}
        if single:
            topic_vars["dp"] = str(dp)
        topic = pb.render_template(tpls.event, topic_vars)
        if single:
            payload = pb.render_template(tpls.payload, {"value": json.dumps(value)})
            check = pb.parse_seed_dps(payload, str(dp), tpls.payload)
        else:
            payload = json.dumps({str(dp): value})
            check = pb.parse_seed_dps(payload, None, tpls.payload)
        if check != {str(dp): value}:
            raise RuntimeError(
                f"derived payload not byte-faithful under template {tpls.payload!r}: "
                f"parsed back as {check!r}, expected {{{str(dp)!r}: {value!r}}}"
            )
        return topic, payload

    async def publish_derived_dp(
        self, device_id: str, dp: str, value: Any, *, retain: bool
    ) -> None:
        """Publish a derived DP value on the `{type}=derived` topic for `device_id`.

        The manager renders both topic and payload (byte-faithful, see
        `_render_derived`); the plugin never touches bridge config."""
        if self.state.templates is None:
            raise RuntimeError("templates not yet resolved (bootstrap incomplete)")
        topic, payload = self._render_derived(self.state.templates, device_id, str(dp), value)
        await self.publish_raw(topic, payload, retain=retain)

    async def clear_derived_dp(self, device_id: str, dp: str) -> None:
        """Clear a derived DP: empty retained payload on its topic (the
        rule-level cleanup path). Always `retain=True` — the canonical
        retained-clear, identical to the bridge's own scavenger."""
        if self.state.templates is None:
            raise RuntimeError("templates not yet resolved (bootstrap incomplete)")
        # value is irrelevant to the topic; pass a placeholder of the right type.
        topic, _ = self._render_derived(self.state.templates, device_id, str(dp), 0)
        await self.publish_raw(topic, "", retain=True)

    def subscribe_scanner(self) -> asyncio.Queue[dict[str, Any]]:
        """Returns a fresh queue that will receive every scanner-topic
        payload from now until `unsubscribe_scanner(q)` is called.

        Synchronous (no I/O) so callers can pair it with `finally:` in a
        `try`/`finally` block — the bridge's publish dispatch already
        runs `q.put_nowait()` against whatever's in the list at message
        time. Leaking a subscriber would grow per-scan memory because
        every sighting fans out to every queue.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._scanner_subscribers.append(q)
        return q

    def unsubscribe_scanner(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a queue previously returned by `subscribe_scanner()`.
        Idempotent: calling twice (or against a queue that was never
        registered) is a no-op so cleanup paths don't have to guard
        against re-entry."""
        try:
            self._scanner_subscribers.remove(q)
        except ValueError:
            pass
