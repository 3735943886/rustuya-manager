"""Bootstrap + cloud reload leak regression pins.

Two surfaces:

  1. BridgeClient creation + `_bootstrap_done` event set/clear cycles —
     pins that no Future or Task hangs off the Event after the client is
     dropped. The bootstrap_guard task that the reconnect loop may spawn
     is the historical risk site (it awaits `wait_for(_bootstrap_done)`
     so a missed cancel path would leak the guard task across cycles).
  2. Cloud JSON reload — every `_load_cloud` allocates a fresh device
     graph. If anything caches Device instances by id outside the
     intended dict, repeated reloads would accumulate. This is also the
     surface the wizard hits when fresh devices come back from the cloud.
"""

from __future__ import annotations

import json
from pathlib import Path

from rustuya_manager.cloud import load_cloud_file
from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import State

from .conftest import assert_no_leak, assert_no_leak_async


async def test_bootstrap_event_lifecycle_no_leak():
    """N=100 BridgeClient construct + _bootstrap_done.set cycles must stay flat.

    No async context is entered, so no reconnect task is spawned. We're
    measuring the per-instance overhead of constructing a BridgeClient,
    setting the bootstrap event, and dropping the reference. Anything that
    accumulates here would show up immediately at scale.
    """

    def one_cycle():
        state = State()
        client = BridgeClient(
            broker="mqtt://localhost:1883",
            root="rustuya",
            state=state,
        )
        client._bootstrap_done.set()
        del client
        del state

    for _ in range(3):
        one_cycle()
    async with assert_no_leak_async(
        max_kb=80,
        max_objects=400,
        max_tasks=2,
        label="BridgeClient bootstrap event lifecycle",
    ):
        for _ in range(100):
            one_cycle()


def test_cloud_reload_no_leak(tmp_path: Path):
    """N=50 cloud JSON reloads must not accumulate Device objects.

    Each `load_cloud_file` parses the same file and returns a fresh
    {id: Device} dict. Caller (caller is the test here) drops the dict
    every cycle, so the entire Device graph should be reclaimed.
    """
    devices = [
        {
            "id": f"bf-{i:04x}",
            "name": f"device-{i}",
            "local_key": "k" * 16,
            "ip": f"192.168.1.{i}",
        }
        for i in range(8)
    ]
    cloud_file = tmp_path / "cloud.json"
    cloud_file.write_text(json.dumps(devices))
    for _ in range(3):
        load_cloud_file(cloud_file)
    with assert_no_leak(
        max_kb=80,
        max_objects=400,
        max_tasks=0,
        label="cloud JSON reload",
    ):
        for _ in range(50):
            load_cloud_file(cloud_file)
