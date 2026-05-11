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
import time
from typing import Any, Awaitable, Callable

import paho.mqtt.client as mqtt
import pyrustuyabridge as pb

from .models import Device
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
        on_event: Callable[[str, dict[str, str], Any], Awaitable[None]] | None = None,
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

    # ── paho callbacks (run on paho's loop thread) ───────────────────────
    def _on_connect(self, client: mqtt.Client, *_args, **_kw) -> None:
        cfg_topic = BRIDGE_CONFIG_TOPIC_TPL.replace("{root}", self.root)
        client.subscribe(cfg_topic)
        logger.info("Subscribed to bridge config: %s", cfg_topic)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (msg.topic, payload))

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
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id)
        except AttributeError:
            c = mqtt.Client(client_id=self._client_id)  # paho < 2.0 fallback
        c.on_connect = self._on_connect
        c.on_message = self._on_message
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
            topic, payload = await self._queue.get()
            try:
                await self._dispatch(topic, payload)
            except Exception:  # noqa: BLE001 - log and keep running, never let the loop die
                logger.exception("Failed to handle MQTT message on %s", topic)

    async def _dispatch(self, topic: str, payload: str) -> None:
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

        await self._route(matched_as, vars_, parsed)

    def _resolve_device_key(
        self, vars_: dict[str, str], parsed: dict[str, Any]
    ) -> str | None:
        """Find the device's bridge ID, falling back to name. Custom event
        topics may carry only {name}, so we look the ID up in bridge state;
        if even that isn't known yet, we return the name itself as a
        stable enough key for the live DPS map."""
        did = vars_.get("id") or parsed.get("id")
        if did:
            return str(did)
        name = vars_.get("name") or parsed.get("name")
        if not name:
            return None
        # Reverse lookup: find a bridge device with that name
        for dev in self.state.bridge.values():
            if dev.name == name:
                return dev.id
        return str(name)

    async def _route(
        self, matched_as: str, vars_: dict[str, str], parsed: Any
    ) -> None:
        """Updates State based on what kind of message arrived."""
        if matched_as == "message":
            # Response or error reply. The bridge's `status` action has the
            # full device list in `devices`; record it.
            if isinstance(parsed, dict):
                target = vars_.get("id", "bridge")
                if parsed.get("action") == "status" and isinstance(parsed.get("devices"), dict):
                    bridge_devs = {
                        did: Device.from_dict(d) for did, d in parsed["devices"].items()
                    }
                    await self.state.set_bridge(bridge_devs)
                await self.state.record_response(target, parsed)
        elif matched_as == "event":
            # Device DPS update. parse_payload merges {dp}/{value} into a dps dict.
            if isinstance(parsed, dict):
                key = self._resolve_device_key(vars_, parsed)
                dps = parsed.get("dps")
                if key and isinstance(dps, dict):
                    await self.state.merge_dps(key, dps)
        # scanner messages — surfaced via the optional on_event callback only

        if self._on_event is not None:
            await self._on_event(matched_as, vars_, parsed)

    # ── bridge-config handling ──────────────────────────────────────────
    async def _on_bridge_config(self, payload: str) -> None:
        try:
            cfg = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in bridge/config: %s", e)
            return

        # Bridge may publish its own root; honor it.
        root = cfg.get("mqtt_root_topic") or self.root
        self.root = root

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
        await self.state.set_templates(templates)
        await self._subscribe_runtime_topics(templates)
        await self._request_initial_status(templates)
        self._bootstrap_done.set()
        logger.info("Bootstrap complete — templates resolved for root %r", root)

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
        self._bootstrap_done.set()

    async def _subscribe_runtime_topics(self, t: BridgeTemplates) -> None:
        assert self._client is not None
        for tpl in (t.event, t.message, t.scanner):
            wildcard = pb.tpl_to_wildcard(tpl, t.root)
            self._client.subscribe(wildcard)
            logger.info("Subscribed: %s", wildcard)

    async def _request_initial_status(self, t: BridgeTemplates) -> None:
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
