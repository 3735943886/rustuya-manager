"""Async MQTT client that talks to a running rustuya-bridge.

Bridges paho-mqtt's callback-based API to asyncio: callbacks push messages onto
an `asyncio.Queue` via `call_soon_threadsafe`, then an async consumer task uses
`pyrustuyabridge` helpers to parse the topic+payload identically to how the
bridge would. Parsed events are written into the shared `State`.

Bootstrap order (mirrors the bridge's contract):
  1. Connect to the broker.
  2. Subscribe to `{root}/bridge/config` (retained). The bridge publishes its
     resolved config there at startup, so this is the source of truth for the
     user's customised topic/payload templates.
  3. Once the retained config arrives, derive the post-`{root}` templates and
     subscribe to event/message/scanner wildcards.
  4. Publish a `status` command and wait for the bridge's reply to populate
     the initial device list.

Notes on threading:
  paho's `on_message` runs on paho's internal loop thread. Any state mutation
  must hop back to the asyncio loop via `call_soon_threadsafe`. We do that by
  pushing raw (topic, payload) tuples onto an asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import paho.mqtt.client as mqtt
import pyrustuyabridge as pb

from .models import Device
from .payload import parse_payload_with_template, validate_payload_template
from .state import BridgeTemplates, State

logger = logging.getLogger(__name__)

BRIDGE_CONFIG_TOPIC_TPL = "{root}/bridge/config"
BOOTSTRAP_TIMEOUT_SEC = 5.0


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
    """Owns the paho-mqtt client and the async receive loop.

    Public API:
        await client.run()                 # bootstrap + run until stopped
        await client.publish_command(...)  # forward-render command topic and publish
        await client.stop()
    """

    def __init__(
        self,
        broker: str,
        root: str,
        state: State,
        *,
        client_id: str = "rustuya-manager",
        on_event: Callable[[str, dict[str, str], Any, dict[str, Any] | None], Awaitable[None]] | None = None,
    ) -> None:
        host, port = _parse_broker_url(broker)
        self.host = host
        self.port = port
        self.root = root
        self.state = state
        self._client_id = client_id
        self._on_event = on_event

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._client: mqtt.Client | None = None
        self._stopped = asyncio.Event()
        self._bootstrap_done = asyncio.Event()
        # Cache of the runtime wildcards we want to keep subscribed. Refreshed
        # whenever templates are resolved. The on_connect callback replays this
        # list on every (re)connect so subscriptions survive broker hiccups
        # even with the paho default clean_session=True.
        self._runtime_wildcards: list[str] = []

    # ── paho v2 callbacks (run on paho's loop thread) ────────────────────
    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        # `reason_code` is a paho ReasonCode object; its is_failure attribute
        # is True on a refused/errored CONNACK.
        if reason_code.is_failure:
            logger.error("MQTT CONNACK failed: %s", reason_code)
            return

        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", self.root)
        rc, _mid = client.subscribe(cfg_topic)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Subscribe FAILED rc=%s for %s", rc, cfg_topic)
        else:
            logger.info("Subscribed to bridge config: %s", cfg_topic)

        # Replay runtime subscriptions on reconnect. First connect has an empty
        # list; subsequent connects after bootstrap re-establish the wildcards.
        for wildcard in self._runtime_wildcards:
            rc, _mid = client.subscribe(wildcard)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error("Re-subscribe FAILED rc=%s for %s", rc, wildcard)
            else:
                logger.info("Re-subscribed: %s", wildcard)

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        # paho v2 signature. `reason_code` is a ReasonCode object whose
        # `is_failure` is False for "Normal disconnection" (the clean path
        # triggered by client.disconnect()) and True for any broker-side
        # disconnect we should auto-reconnect from.
        if reason_code is None or not reason_code.is_failure:
            logger.info("MQTT disconnected cleanly")
        else:
            logger.warning("MQTT disconnected unexpectedly: %s (paho will retry)", reason_code)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        # Carry the retain flag through to listeners — the CLI suppresses prints
        # for retained messages so the initial subscribe burst (which can be
        # hundreds of lines on a busy broker) doesn't drown live activity.
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, (msg.topic, payload, bool(msg.retain))
            )

    # ── lifecycle ────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Connect, bootstrap, then process incoming messages until `stop()`."""
        self._loop = asyncio.get_running_loop()
        self._client = self._make_client()
        self._client.connect(self.host, self.port)
        self._client.loop_start()

        try:
            consumer_task = asyncio.create_task(self._consume_loop())
            bootstrap_task = asyncio.create_task(self._bootstrap())
            await self._stopped.wait()
            consumer_task.cancel()
            bootstrap_task.cancel()
            await asyncio.gather(consumer_task, bootstrap_task, return_exceptions=True)
        finally:
            self._client.loop_stop()
            self._client.disconnect()

    async def stop(self) -> None:
        self._stopped.set()

    def _make_client(self) -> mqtt.Client:
        # paho-mqtt 2.0+ is required (see pyproject); v1 has a different
        # callback signature and isn't supported here.
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        # Bound exponential backoff for transient broker outages.
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        return c

    # ── async loops ──────────────────────────────────────────────────────
    async def _bootstrap(self) -> None:
        """Wait for the retained bridge/config, derive templates, subscribe, and
        request initial status. Bails out cleanly on timeout (CLI keeps the
        retained-fallback option to use defaults)."""
        try:
            await asyncio.wait_for(self._bootstrap_done.wait(), BOOTSTRAP_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout waiting for %s retained config — bridge may be offline. "
                "Falling back to bridge defaults.",
                BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", self.root),
            )
            await self._apply_default_templates()

    async def _consume_loop(self) -> None:
        while True:
            item = await self._queue.get()
            # Older callers may queue (topic, payload); accept both shapes.
            if len(item) == 3:
                topic, payload, retain = item
            else:
                topic, payload = item
                retain = False
            try:
                await self._dispatch(topic, payload, retain=retain)
            except Exception:  # noqa: BLE001 - log and keep running, never let the loop die
                logger.exception("Failed to handle MQTT message on %s", topic)

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

    def _resolve_device_key(
        self, vars_: dict[str, str], parsed: dict[str, Any]
    ) -> str | None:
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
            vars_, parsed.get("id"), name,
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
                    bridge_devs = {
                        did: Device.from_dict(d) for did, d in parsed["devices"].items()
                    }
                    await self.state.set_bridge(bridge_devs)
                # The bridge publishes per-device connection state under the
                # `error` level: errorCode=0 means "Connection Successful",
                # any non-zero code means the device is unreachable / errored.
                if level == "error" and target != "bridge" and "errorCode" in parsed:
                    code = parsed.get("errorCode")
                    msg = parsed.get("errorMsg") or parsed.get("payloadStr") or ""
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

    async def _subscribe_runtime_topics(self, t: BridgeTemplates) -> None:
        assert self._client is not None
        # Recompute the wildcard list and cache it for reconnect replay.
        wildcards = [pb.tpl_to_wildcard(tpl, t.root) for tpl in (t.event, t.message, t.scanner)]
        # Deduplicate while preserving order — custom templates can collapse to
        # the same wildcard (e.g. event {root}/x/{id} and message {root}/x/{id}).
        seen: set[str] = set()
        new_wildcards = [w for w in wildcards if not (w in seen or seen.add(w))]

        # Unsubscribe wildcards we no longer need.
        for stale in self._runtime_wildcards:
            if stale not in new_wildcards:
                self._client.unsubscribe(stale)
                logger.info("Unsubscribed: %s", stale)

        # Subscribe only the newcomers — re-subscribing an existing wildcard
        # forces the broker to re-deliver every retained message, which is
        # both wasteful and (in the case of bridge/config) the trigger for an
        # infinite bootstrap loop.
        previously = set(self._runtime_wildcards)
        for wildcard in new_wildcards:
            if wildcard in previously:
                continue
            rc, _mid = self._client.subscribe(wildcard)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error("Subscribe FAILED rc=%s for %s", rc, wildcard)
            else:
                logger.info("Subscribed: %s", wildcard)

        self._runtime_wildcards = new_wildcards

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
        carried."""
        if self._client is None:
            raise RuntimeError("MQTT client not connected")
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
        self._client.publish(topic, body, qos=1)
