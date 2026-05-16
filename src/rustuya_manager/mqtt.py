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
from typing import Any

import aiomqtt
import pyrustuyabridge as pb

from .models import Device
from .payload import parse_payload_with_template, validate_payload_template
from .state import BridgeTemplates, State

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


def _parse_broker_url(broker: str) -> tuple[str, int]:
    """Accepts 'mqtt://host:port' or 'host:port' or 'host'."""
    if "://" in broker:
        broker = broker.split("://", 1)[1]
    # strip optional user:pass@
    if "@" in broker:
        broker = broker.split("@", 1)[1]
    if ":" in broker:
        host, port_s = broker.rsplit(":", 1)
        return host, int(port_s)
    return broker, 1883


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
    ) -> None:
        host, port = _parse_broker_url(broker)
        self.host = host
        self.port = port
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
                    async with aiomqtt.Client(
                        hostname=self.host,
                        port=self.port,
                        identifier=self._client_id,
                        max_queued_incoming_messages=self._MAX_QUEUED_INCOMING,
                    ) as client:
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
                    bridge_devs = {did: Device.from_dict(d) for did, d in parsed["devices"].items()}
                    await self.state.set_bridge(bridge_devs)
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
                        await self.state.record_response(target, parsed)
                elif action == "add" and status_val == "ok":
                    # The add ack doesn't carry the device fields the bridge
                    # ended up storing; ask for a status refresh so state.bridge
                    # picks up the new/updated entry authoritatively.
                    await self.state.record_response(target, parsed)
                    asyncio.create_task(self.publish_command("status", target_id="bridge"))
                else:
                    await self.state.record_response(target, parsed)
        elif matched_as == "event":
            if isinstance(parsed, dict):
                key = self._resolve_device_key(vars_, parsed)
                dps = await self._extract_dps_from_event(vars_, parsed, payload_str)
                if key and dps:
                    await self.state.merge_dps(key, dps)
                    # Data flowing means the device is alive — mark online.
                    # Leave message empty: the bridge's error-channel sends
                    # human-readable strings like "Connection Successful",
                    # and event-derived liveness has no equivalent to put
                    # in the MSG row. The online dot + edge color already
                    # convey state; MSG should only fire for real diagnostics.
                    await self.state.set_live_status(key, "online", code=0, message="")
                # Surface the resolved key+dps to listeners (CLI prints these).
                extras["device_id"] = key
                extras["dps"] = dps
        # scanner messages — surfaced via the optional on_event callback only

        if self._on_event is not None:
            await self._on_event(matched_as, vars_, parsed, extras)

    async def _extract_dps_from_event(
        self, vars_: dict[str, str], parsed: dict[str, Any], payload_str: str
    ) -> dict[str, Any] | None:
        """Returns a {dp: value} map extracted from an event payload, or None.

        Order of attempts:
          1. parse_payload_with_template — JSON tree walk against the user's
             `mqtt_payload_template`. Handles arbitrary JSON-shaped templates
             with any combination of {value}, {dps}, {name}, etc.
          2. Bridge's parse_mqtt_payload already produced a `dps` dict (the
             bare-scalar template case: payload `true` + topic {dp}=1 →
             {"dps":{"1":true}}). Use it.

        If neither works, returns None — the user's template isn't a shape
        the manager can read, and a warning has already been raised at
        bootstrap to nudge them to fix it."""
        tpls = self.state.templates
        if tpls and payload_str:
            captures = parse_payload_with_template(payload_str, tpls.payload)
            if captures:
                if isinstance(captures.get("dps"), dict):
                    return captures["dps"]
                if "value" in captures and vars_.get("dp"):
                    return {vars_["dp"]: captures["value"]}

        # Fall back to whatever the bridge's parse_payload produced.
        dps = parsed.get("dps")
        if isinstance(dps, dict) and dps:
            return dps
        return None

    # ── bridge-config handling ──────────────────────────────────────────
    async def _on_bridge_config(self, payload: str) -> None:
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
        await self._subscribe_runtime_topics(templates)
        await self._validate_payload_template(templates.payload)
        # Bridge is alive — clear the offline placeholder warning if it was
        # set by an earlier default-templates fallback.
        await self.state.clear_warning("bridge_offline")
        if not self._bootstrap_done.is_set():
            await self._request_initial_status()
            self._bootstrap_done.set()
            logger.info("Bootstrap complete — templates resolved for root %r", root)
        else:
            logger.info("Bridge config updated — templates re-resolved for root %r", root)

    async def _validate_payload_template(self, template: str | None) -> None:
        """Surface a UI warning when the bridge's payload template isn't a
        shape the manager can extract from. The fix is at the bridge config
        level (mqtt_payload_template), not in the manager."""
        ok, message = validate_payload_template(template)
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
