"""End-to-end test against a real rustuya-bridge + mosquitto.

Verifies the full MQTT cycle that the user has explicitly flagged as the
highest-risk area:

  1. Bootstrap reads `{root}/bridge/config` retained
  2. Manager subscribes to runtime wildcards derived from custom templates
  3. Adding a device via mosquitto_pub propagates into state.bridge
  4. DPS events arrive and merge into state.dps
  5. publish_command actually reaches the bridge (the command_topic round-trip)
  6. After forcing a paho disconnect + reconnect, subscriptions still work
     (the runtime wildcards must be re-played by on_connect)

The bridge under test is `pyrustuyabridge.PyBridgeServer` running in a daemon
thread — same Rust core as the bridge binary, just embedded via the Python
bindings instead of spawned as a subprocess. That keeps CI free of the Rust
toolchain while still exercising the byte-identical MQTT/parsing path the
production bridge uses.

The whole module skips if mosquitto is not reachable on localhost:1883.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pyrustuyabridge as pb
import pytest

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import State


def _broker_reachable() -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", 1883))
        s.close()
        return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(not _broker_reachable(), reason="no MQTT broker on localhost:1883"),
    pytest.mark.skipif(not shutil.which("mosquitto_pub"), reason="mosquitto_pub not installed"),
]

# Use a dedicated root per test to avoid clobbering anything else running on
# the box. PID + epoch suffix prevents a re-run from colliding with leftover
# retained state from a previous run.
ROOT = "rustuya_mgr_e2e_test"


def _spawn_bridge(root: str, tmp_path: Path) -> tuple[object, threading.Thread]:
    """Start an embedded bridge with deliberately-custom templates so we
    exercise the binding-backed parsing path (not the default templates)."""
    state_file = tmp_path / "bridge_state.json"
    server = pb.PyBridgeServer(
        mqtt_broker="mqtt://localhost:1883",
        mqtt_root_topic=root,
        mqtt_event_topic="{root}/dev/{name}/dp/{dp}/state",
        mqtt_message_topic="tuyalog_test/{level}/{id}",
        mqtt_command_topic="{root}/cmd/{id}/{action}",
        state_file=str(state_file),
        log_level="warn",
    )
    # `start` blocks until close()/SIGINT — run it on a daemon thread so the
    # test stays in control. Daemon ensures any leaked instance dies with
    # the pytest process; teardown still tries a clean close() first.
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    return server, thread


def _wait_for_retained_config(root: str, timeout: float = 5.0) -> bool:
    """Spin until mosquitto_sub can fetch the retained bridge/config."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                [
                    "mosquitto_sub",
                    "-h",
                    "localhost",
                    "-t",
                    f"{root}/bridge/config",
                    "-C",
                    "1",
                    "-W",
                    "1",
                ],
                timeout=2,
            )
            if out:
                return True
        except subprocess.SubprocessError:
            pass
        time.sleep(0.2)
    return False


def _kill_bridge(server: object, thread: threading.Thread) -> None:
    # `close()` schedules cleanup on an asyncio loop, so we wrap it in a
    # short-lived loop — without that it raises "no running event loop".
    try:
        asyncio.run(_close_async(server))
    except Exception:
        pass
    thread.join(timeout=3)


async def _close_async(server: object) -> None:
    server.close()
    # Give the bridge's tokio runtime a beat to flush its cleanup before
    # the asyncio loop tears down underneath it.
    await asyncio.sleep(0.05)


@pytest.fixture
def bridge(tmp_path):
    """Per-test bridge with a unique root, cleaned up on teardown."""
    root = f"{ROOT}_{os.getpid()}_{int(time.time())}"
    # Pre-clean the retained config so any stale state doesn't trip detection.
    subprocess.run(
        ["mosquitto_pub", "-h", "localhost", "-t", f"{root}/bridge/config", "-r", "-n"],
        check=False,
    )
    server, thread = _spawn_bridge(root, tmp_path)
    try:
        assert _wait_for_retained_config(root), "bridge did not publish retained config in time"
        yield root
    finally:
        _kill_bridge(server, thread)
        # Belt and braces: clear the retained config explicitly so even a
        # half-closed server doesn't leave a poisoned topic behind.
        subprocess.run(
            ["mosquitto_pub", "-h", "localhost", "-t", f"{root}/bridge/config", "-r", "-n"],
            check=False,
        )


async def _run_client_briefly(client: BridgeClient, fn) -> None:
    """Helper: run client.run() as a task, call fn() while it's alive, then stop."""
    task = asyncio.create_task(client.run())
    try:
        # Wait for bootstrap; give it more slack than the client's 5s internal timeout.
        await asyncio.wait_for(client._bootstrap_done.wait(), 7.0)
        await fn()
    finally:
        await client.stop()
        await asyncio.wait_for(task, 5.0)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_bootstrap_resolves_custom_templates(bridge: str):
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)

    async def check():
        assert state.templates is not None
        assert state.templates.event == f"{bridge}/dev/{{name}}/dp/{{dp}}/state"
        assert state.templates.message == "tuyalog_test/{level}/{id}"

    await _run_client_briefly(client, check)


async def test_add_device_propagates_to_bridge_state(bridge: str):
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)

    async def check():
        # Use the bridge's custom command topic to add a device
        subprocess.run(
            [
                "mosquitto_pub",
                "-h",
                "localhost",
                "-t",
                f"{bridge}/cmd/bf-test-1/add",
                "-m",
                '{"key":"k1234567890abcdef","ip":"10.0.0.1"}',
            ],
            check=True,
        )
        # Trigger a status query so the bridge replies with the device list
        await client.publish_command("status", target_id="bridge")
        # Give the response time to land
        for _ in range(40):
            await asyncio.sleep(0.1)
            if "bf-test-1" in state.bridge:
                break
        assert "bf-test-1" in state.bridge, f"bridge devices: {list(state.bridge.keys())}"
        assert state.bridge["bf-test-1"].ip == "10.0.0.1"

    await _run_client_briefly(client, check)


async def test_dps_event_arrives_through_custom_topic(bridge: str):
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)

    async def check():
        # Add a device on the bridge AND populate the manager's view of it via
        # a status round-trip. Without this, the event below would carry a
        # device `name` the manager has never seen — _resolve_device_key
        # correctly drops such events to avoid phantom DPS entries.
        subprocess.run(
            [
                "mosquitto_pub",
                "-h",
                "localhost",
                "-t",
                f"{bridge}/cmd/bf-test-evt/add",
                "-m",
                '{"key":"k1234567890abcdef","ip":"10.0.0.1","name":"living_room"}',
            ],
            check=True,
        )
        await client.publish_command("status", target_id="bridge")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if "bf-test-evt" in state.bridge:
                break
        assert "bf-test-evt" in state.bridge, (
            f"manager never learned about the device: {list(state.bridge.keys())}"
        )

        # Simulate a DPS update being published on the custom event topic.
        # In reality the bridge itself publishes these from device data; here
        # we synthesise the same shape to verify the manager's match/parse
        # pipeline accepts the topic+payload pair and resolves `name` →
        # the bridge id `bf-test-evt`.
        subprocess.run(
            [
                "mosquitto_pub",
                "-h",
                "localhost",
                "-t",
                f"{bridge}/dev/living_room/dp/1/state",
                "-m",
                "true",
            ],
            check=True,
        )
        for _ in range(30):
            await asyncio.sleep(0.1)
            if state.dps.get("bf-test-evt"):
                break

        # Pinning the resolved key catches a regression of the phantom-key
        # fallback that 0.3.1 removed: if DPS shows up under "living_room"
        # instead of "bf-test-evt", we lost the reverse-lookup behavior.
        assert state.dps.get("bf-test-evt") == {"1": True}, f"dps: {state.dps}"

    await _run_client_briefly(client, check)


async def test_reconnect_preserves_subscriptions(bridge: str):
    """Simulate a broker-side disconnect by closing the paho socket directly,
    then wait for paho's auto-reconnect to kick in. Verify the manager still
    receives events afterward — this is the bug the runtime-wildcards cache
    prevents.

    We use _sock.close() instead of client.disconnect()+reconnect() because the
    former is what actually happens when a broker hiccups in production; the
    latter is a clean disconnect that paho doesn't auto-recover from."""
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)

    async def check():
        # Sanity: runtime subscriptions should have been recorded after bootstrap
        assert client._runtime_wildcards, "no runtime wildcards cached"
        initial_subs = list(client._runtime_wildcards)

        # Pre-add a device + status round-trip BEFORE the disconnect so the
        # manager knows the device name. Otherwise the event we publish after
        # reconnect would be silently dropped at _resolve_device_key, masking
        # the actual subscription-survival question this test is asking.
        subprocess.run(
            [
                "mosquitto_pub",
                "-h",
                "localhost",
                "-t",
                f"{bridge}/cmd/bf-reconnect-dev/add",
                "-m",
                '{"key":"k1234567890abcdef","ip":"10.0.0.1","name":"post_reconnect_dev"}',
            ],
            check=True,
        )
        await client.publish_command("status", target_id="bridge")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if "bf-reconnect-dev" in state.bridge:
                break
        assert "bf-reconnect-dev" in state.bridge, (
            f"manager never learned about the device: {list(state.bridge.keys())}"
        )

        # Slam the socket shut. paho's loop will observe the broken pipe, fire
        # on_disconnect with a non-zero reason, then auto-reconnect (we set
        # min_delay=1 in _make_client).
        assert client._client is not None
        sock = getattr(client._client, "_sock", None)
        if sock is None:
            pytest.skip("paho doesn't expose _sock on this version")
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

        # Auto-reconnect has min_delay=1s; give it plenty of slack.
        # We poll for the reconnect by trying a round-trip with a fresh event.
        for _attempt in range(40):
            await asyncio.sleep(0.25)
            subprocess.run(
                [
                    "mosquitto_pub",
                    "-h",
                    "localhost",
                    "-t",
                    f"{bridge}/dev/post_reconnect_dev/dp/7/state",
                    "-m",
                    "42",
                ],
                check=True,
            )
            await asyncio.sleep(0.1)
            if state.dps.get("bf-reconnect-dev", {}).get("7") == 42:
                break
        assert state.dps.get("bf-reconnect-dev", {}).get("7") == 42, (
            f"event still not received after reconnect — runtime subs lost. "
            f"cached wildcards: {initial_subs}, dps: {state.dps}"
        )

    await _run_client_briefly(client, check)


async def test_remove_clears_per_device_state(bridge: str):
    """A successful `remove` ack from the bridge should drop the device id
    from every per-device bucket in the manager (bridge / dps / live /
    last_seen / last_response). Otherwise a device that transitions to
    "missing" (cloud-only) would still display its prior runtime data."""
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)

    async def check():
        # Add a device with a name, populate runtime state via a DPS event,
        # confirm the manager has full per-device state for it.
        subprocess.run(
            [
                "mosquitto_pub",
                "-h",
                "localhost",
                "-t",
                f"{bridge}/cmd/bf-rm-dev/add",
                "-m",
                '{"key":"k1234567890abcdef","ip":"10.0.0.1","name":"removable"}',
            ],
            check=True,
        )
        await client.publish_command("status", target_id="bridge")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if "bf-rm-dev" in state.bridge:
                break
        assert "bf-rm-dev" in state.bridge

        subprocess.run(
            [
                "mosquitto_pub",
                "-h",
                "localhost",
                "-t",
                f"{bridge}/dev/removable/dp/1/state",
                "-m",
                "true",
            ],
            check=True,
        )
        for _ in range(30):
            await asyncio.sleep(0.1)
            if state.dps.get("bf-rm-dev"):
                break
        assert state.dps.get("bf-rm-dev") == {"1": True}

        # Now publish a remove. The bridge replies with action=remove,
        # status=ok on its message topic, which the manager routes into
        # state.remove_device.
        await client.publish_command("remove", target_id="bf-rm-dev")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if "bf-rm-dev" not in state.bridge:
                break

        # Every per-device bucket should be empty for the removed id.
        assert "bf-rm-dev" not in state.bridge, f"bridge: {list(state.bridge.keys())}"
        assert "bf-rm-dev" not in state.dps, f"dps: {state.dps}"
        assert "bf-rm-dev" not in state.live_status, f"live: {state.live_status}"
        assert "bf-rm-dev" not in state.last_seen, f"last_seen: {state.last_seen}"
        assert "bf-rm-dev" not in state.last_response, f"last_response: {state.last_response}"

    await _run_client_briefly(client, check)


async def _run_embed_test(tmp_path, root: str, *, bridge_config: str | None = None):
    """Shared scaffolding for embed-bridge e2e variants.

    Spawns an embedded bridge on the given root, runs the manager against it,
    waits for bootstrap, then cleanly tears down. Yields the `(state, args)`
    tuple to the test body inside the bootstrapped window so the caller can
    assert on state.templates etc."""
    from types import SimpleNamespace

    from rustuya_manager.cli import _close_embedded_bridge, _resolve_embedded_bridge

    subprocess.run(
        ["mosquitto_pub", "-h", "localhost", "-t", f"{root}/bridge/config", "-r", "-n"],
        check=False,
    )
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=root, state=state)
    args = SimpleNamespace(
        embed_bridge=True,
        broker="mqtt://localhost:1883",
        root=root,
        cloud=str(tmp_path / "tuyadevices.json"),
        bridge_state=str(tmp_path / "bridge-state.json"),
        log_level="warn",
        bridge_config=bridge_config,
    )

    embedded = None
    run_task = asyncio.create_task(client.run())
    try:
        embedded = await _resolve_embedded_bridge(state, args)
        assert embedded is not None, "embed-bridge should have spawned"
        assert "embedded_bridge_aborted" not in state.warnings
        await asyncio.wait_for(client._bootstrap_done.wait(), 7.0)
        return state, args
    finally:
        # Caller's assertions ran (or raised); shut everything down regardless.
        await client.stop()
        await asyncio.wait_for(run_task, 5.0)
        if embedded is not None:
            server, thread = embedded
            await _close_embedded_bridge(server)
            thread.join(timeout=3)
        subprocess.run(
            ["mosquitto_pub", "-h", "localhost", "-t", f"{root}/bridge/config", "-r", "-n"],
            check=False,
        )


async def test_embed_bridge_spawns_when_no_external(tmp_path):
    """`--embed-bridge` should bring up a PyBridgeServer in the same process
    when no other bridge owns the root. Once it publishes its retained
    config, the manager's normal bootstrap path picks it up exactly as if
    it were an external bridge."""
    root = f"{ROOT}_embed_{os.getpid()}_{int(time.time())}"
    state, _args = await _run_embed_test(tmp_path, root)
    assert state.templates is not None
    assert state.templates.root == root


async def test_embed_bridge_reads_existing_bridge_config(tmp_path):
    """When --bridge-config points at an existing file, the embedded bridge
    must actually honor it. Pins the pyrustuyabridge >= 0.1.1 contract:
    config_path kwarg → file is read → file values appear in the
    eventually-published bridge/config retained payload → state.templates
    reflects them."""
    import json as _json

    root = f"{ROOT}_embed_cfg_{os.getpid()}_{int(time.time())}"
    cfg_path = tmp_path / "bridge.json"
    # Custom event topic that the bridge's defaults would never produce on
    # their own; this is what proves the file was actually read.
    custom_event = "{root}/custom/dev/{name}/dp/{dp}"
    cfg_path.write_text(_json.dumps({"mqtt_event_topic": custom_event}))

    state, _args = await _run_embed_test(tmp_path, root, bridge_config=str(cfg_path))
    assert state.templates is not None
    # `state.templates.event` is the post-{root}-substituted form; the bridge
    # renders {root} but leaves {name}/{dp} alone for the manager's matcher.
    expected = custom_event.replace("{root}", root)
    assert state.templates.event == expected, (
        f"bridge ignored --bridge-config: templates.event={state.templates.event!r} "
        f"expected={expected!r}"
    )


async def test_embed_bridge_auto_creates_missing_bridge_config(tmp_path):
    """File-missing path: pyrustuyabridge >= 0.1.1 mirrors the binary's
    auto-create behavior. If --bridge-config points at a nonexistent path,
    the bridge writes its current (kwargs+defaults) settings there so the
    next run reads from the same file. Without this, repeat starts would
    silently lose any kwarg overrides the user expected to persist."""
    root = f"{ROOT}_embed_auto_{os.getpid()}_{int(time.time())}"
    # Subdir doesn't exist yet — the bridge must also `mkdir -p` its parent.
    cfg_path = tmp_path / "subdir" / "bridge.json"
    assert not cfg_path.exists()
    assert not cfg_path.parent.exists()

    await _run_embed_test(tmp_path, root, bridge_config=str(cfg_path))

    # File created + populated with serialised config.
    assert cfg_path.exists(), "bridge did not auto-create missing config file"
    import json as _json

    written = _json.loads(cfg_path.read_text())
    # `mqtt_root_topic` is one of the manager-provided kwargs — it must land
    # in the persisted config so subsequent runs reuse the same root.
    assert written.get("mqtt_root_topic") == root, f"persisted config missing root: {written}"


async def test_embed_bridge_inherits_broker_and_root_from_bridge_config(tmp_path):
    """When --bridge-config supplies `mqtt_root_topic` (and broker), the
    manager picks them up as its own defaults so the user doesn't have to
    repeat them on the CLI. Pins the CLI > bridge-config > default
    precedence: CLI flags are left at default here, so the JSON wins.

    Verified end-to-end via state.templates: the embedded bridge boots
    against the root in the JSON, the manager (which uses the same root
    pulled from the JSON) successfully completes bootstrap, and the
    resulting state.templates.root matches what the JSON declared."""
    import json as _json
    from types import SimpleNamespace

    from rustuya_manager.cli import (
        DEFAULT_BROKER,
        DEFAULT_ROOT,
        _apply_bridge_config_defaults,
        _close_embedded_bridge,
        _resolve_embedded_bridge,
    )

    root_in_cfg = f"{ROOT}_embed_inherit_{os.getpid()}_{int(time.time())}"
    # Pre-clean retained so the helper doesn't see leftover config.
    subprocess.run(
        ["mosquitto_pub", "-h", "localhost", "-t", f"{root_in_cfg}/bridge/config", "-r", "-n"],
        check=False,
    )

    cfg_path = tmp_path / "bridge.json"
    cfg_path.write_text(
        _json.dumps({"mqtt_broker": "mqtt://localhost:1883", "mqtt_root_topic": root_in_cfg})
    )

    # Manager-side CLI flags are left at default — the bridge-config is the
    # only place broker/root are specified.
    args = SimpleNamespace(
        embed_bridge=True,
        broker=DEFAULT_BROKER,
        root=DEFAULT_ROOT,
        cloud=str(tmp_path / "tuyadevices.json"),
        bridge_state=str(tmp_path / "bridge-state.json"),
        log_level="warn",
        bridge_config=str(cfg_path),
    )

    # Apply the bridge-config defaults BEFORE building the manager's client.
    _apply_bridge_config_defaults(args)
    assert args.root == root_in_cfg, "manager did not pick up root from bridge-config"
    assert args.broker == "mqtt://localhost:1883"

    state = State()
    client = BridgeClient(broker=args.broker, root=args.root, state=state)
    embedded = None
    run_task = asyncio.create_task(client.run())
    try:
        embedded = await _resolve_embedded_bridge(state, args)
        assert embedded is not None
        await asyncio.wait_for(client._bootstrap_done.wait(), 7.0)
        assert state.templates is not None
        assert state.templates.root == root_in_cfg
    finally:
        await client.stop()
        await asyncio.wait_for(run_task, 5.0)
        if embedded is not None:
            server, thread = embedded
            await _close_embedded_bridge(server)
            thread.join(timeout=3)
        subprocess.run(
            ["mosquitto_pub", "-h", "localhost", "-t", f"{root_in_cfg}/bridge/config", "-r", "-n"],
            check=False,
        )


async def test_embed_bridge_aborts_when_external_exists(bridge: str):
    """Asking for `--embed-bridge` against a root that already has a bridge
    must refuse cleanly with `embedded_bridge_aborted`. The `bridge` fixture
    spawns an external bridge first, so the helper sees retained config
    within the 1s collision-detection window and declines to spawn."""
    from types import SimpleNamespace

    from rustuya_manager.cli import _resolve_embedded_bridge

    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)
    args = SimpleNamespace(
        embed_bridge=True,
        broker="mqtt://localhost:1883",
        root=bridge,
        cloud="ignored.json",
        bridge_state=None,
        log_level="warn",
        bridge_config=None,
    )

    async def check():
        # External bridge is alive (fixture), so templates land via retained.
        # The helper should detect that and refuse to spawn a second bridge.
        result = await _resolve_embedded_bridge(state, args)
        assert result is None, "embed-bridge must NOT spawn when external owns the root"
        assert "embedded_bridge_aborted" in state.warnings
        warn = state.warnings["embedded_bridge_aborted"]
        assert warn["level"] == "error"
        assert bridge in warn["message"]

    await _run_client_briefly(client, check)


async def test_add_response_triggers_status_refresh(bridge: str):
    """A successful `add` response should make the manager re-fetch status
    so state.bridge picks up the new device without waiting for a manual
    refresh. The bridge's add ack carries only {action, id, status} — not
    the device's stored fields — so we need a status round-trip."""
    state = State()
    client = BridgeClient(broker="mqtt://localhost:1883", root=bridge, state=state)

    async def check():
        # Use the manager's own publish_command so the add response routes
        # through the same path the UI would take.
        await client.publish_command(
            "add",
            target_id="bf-auto-add",
            extra={"key": "k1234567890abcdef", "ip": "10.0.0.42", "name": "auto"},
        )
        # We do NOT explicitly issue status — the test pins that the response
        # handler in _route does it for us.
        for _ in range(40):
            await asyncio.sleep(0.1)
            if "bf-auto-add" in state.bridge:
                break
        assert "bf-auto-add" in state.bridge, (
            f"manager didn't refresh state.bridge after add: {list(state.bridge.keys())}"
        )
        assert state.bridge["bf-auto-add"].ip == "10.0.0.42"

    await _run_client_briefly(client, check)
