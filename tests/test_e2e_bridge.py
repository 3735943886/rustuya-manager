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

The test is skipped if:
  - the bridge binary is not at the expected debug path
  - mosquitto is not reachable on localhost:1883
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import State


BRIDGE_BIN = Path(__file__).resolve().parents[2] / "rustuya-bridge" / "target" / "debug" / "rustuya-bridge"


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
    pytest.mark.skipif(not BRIDGE_BIN.exists(), reason="bridge debug binary not built"),
    pytest.mark.skipif(not _broker_reachable(), reason="no MQTT broker on localhost:1883"),
    pytest.mark.skipif(not shutil.which("mosquitto_pub"), reason="mosquitto_pub not installed"),
]

# Use a dedicated root + ports per test to avoid clobbering anything else
# running on the box. Each test gets its own root by using a random suffix.
ROOT = "rustuya_mgr_e2e_test"


def _spawn_bridge(root: str, tmp_path: Path) -> subprocess.Popen:
    """Start a bridge with deliberately-custom templates so we exercise the
    binding-backed parsing path (not the default templates)."""
    state_file = tmp_path / "bridge_state.json"
    log_file = tmp_path / "bridge.log"
    proc = subprocess.Popen(
        [
            str(BRIDGE_BIN),
            "--mqtt-broker", "mqtt://localhost:1883",
            "--mqtt-root-topic", root,
            "--mqtt-event-topic", "{root}/dev/{name}/dp/{dp}/state",
            "--mqtt-message-topic", "tuyalog_test/{level}/{id}",
            "--mqtt-command-topic", "{root}/cmd/{id}/{action}",
            "--state-file", str(state_file),
            "--log-level", "info",
        ],
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
    )
    return proc


def _wait_for_retained_config(root: str, timeout: float = 5.0) -> bool:
    """Spin until mosquitto_sub can fetch the retained bridge/config."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                [
                    "mosquitto_sub", "-h", "localhost",
                    "-t", f"{root}/bridge/config",
                    "-C", "1", "-W", "1",
                ],
                timeout=2,
            )
            if out:
                return True
        except subprocess.SubprocessError:
            pass
        time.sleep(0.2)
    return False


def _kill_bridge(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def bridge(tmp_path):
    """Per-test bridge with a unique root, cleaned up on teardown."""
    # Append PID + time to avoid duplicate-instance detection from
    # leftover broker state across runs.
    root = f"{ROOT}_{os.getpid()}_{int(time.time())}"
    # Pre-clean the retained config so any stale state doesn't trip detection.
    subprocess.run(
        ["mosquitto_pub", "-h", "localhost", "-t", f"{root}/bridge/config", "-r", "-n"],
        check=False,
    )
    proc = _spawn_bridge(root, tmp_path)
    try:
        assert _wait_for_retained_config(root), "bridge did not publish retained config in time"
        yield root
    finally:
        _kill_bridge(proc)
        # Clear the retained config so the next test (or run) starts clean.
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
            ["mosquitto_pub", "-h", "localhost",
             "-t", f"{bridge}/cmd/bf-test-1/add",
             "-m", '{"key":"k1234567890abcdef","ip":"10.0.0.1"}'],
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
        # Add a device first so the bridge knows it
        subprocess.run(
            ["mosquitto_pub", "-h", "localhost",
             "-t", f"{bridge}/cmd/bf-test-evt/add",
             "-m", '{"key":"k1234567890abcdef","ip":"10.0.0.1","name":"living_room"}'],
            check=True,
        )
        await asyncio.sleep(0.5)

        # Simulate a DPS update being published on the custom event topic.
        # NOTE: in reality the bridge itself publishes these from device data,
        # but for this test we synthesise the same publish a real device would
        # cause — what matters is whether the manager's match/parse pipeline
        # accepts the topic + payload pair correctly.
        subprocess.run(
            ["mosquitto_pub", "-h", "localhost",
             "-t", f"{bridge}/dev/living_room/dp/1/state",
             "-m", "true"],
            check=True,
        )
        # Wait for state update
        for _ in range(30):
            await asyncio.sleep(0.1)
            if state.dps:
                break

        # The key may be the device id (if name resolved via bridge state) or
        # the name fallback — either is acceptable, but the dps value must match.
        all_dps = {dp: v for d in state.dps.values() for dp, v in d.items()}
        assert all_dps == {"1": True}, f"dps: {state.dps}"

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
        for attempt in range(40):
            await asyncio.sleep(0.25)
            subprocess.run(
                ["mosquitto_pub", "-h", "localhost",
                 "-t", f"{bridge}/dev/post_reconnect_dev/dp/7/state",
                 "-m", "42"],
                check=True,
            )
            # Settle, then check whether the event made it through
            await asyncio.sleep(0.1)
            all_dps = {dp: v for d in state.dps.values() for dp, v in d.items()}
            if all_dps.get("7") == 42:
                break
        all_dps = {dp: v for d in state.dps.values() for dp, v in d.items()}
        assert all_dps.get("7") == 42, (
            f"event still not received after reconnect — runtime subs lost. "
            f"cached wildcards: {initial_subs}, dps: {state.dps}"
        )

    await _run_client_briefly(client, check)
