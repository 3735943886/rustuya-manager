"""Embedded bridge spawn/close leak pin (mock-only).

The real `pyrustuyabridge.PyBridgeServer` import pulls in a Rust tokio
runtime + ssl + several MB of C heap, and tracemalloc can't see across the
PyO3 boundary, so a real-import N-cycle test would be slow AND blind.

Instead we patch `pyrustuyabridge.PyBridgeServer` to a MagicMock and pin the
**Python wiring** in `cli._spawn_embedded_bridge` / `_close_embedded_bridge`:
threading.Thread creation, MagicMock closure retention, args reference
retention. If a future change starts holding the `args` namespace or the
Thread object in a module-global cache, the budget breaks immediately.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from rustuya_manager.cli import _close_embedded_bridge, _spawn_embedded_bridge

from .conftest import assert_no_leak_async


def _fake_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        broker="mqtt://localhost:1883",
        root="rustuya",
        log_level="info",
        cloud=str(tmp_path / "tuyadevices.json"),
        bridge_state=str(tmp_path / "rustuya.json"),
        bridge_config=None,
    )


async def test_embed_resolve_cycle_no_leak(tmp_path: Path):
    """N=20 spawn+close cycles (Python wiring only) must stay flat.

    PyBridgeServer is patched to a MagicMock so no Rust thread spins up —
    we only measure threading.Thread creation, args retention, and the
    mock-server stop() path. The thread targets `server.start` which
    returns immediately, so each cycle is dominated by Thread setup/teardown.
    """
    args = _fake_args(tmp_path)

    async def one_cycle():
        with patch("pyrustuyabridge.PyBridgeServer", return_value=MagicMock()):
            server, thread = _spawn_embedded_bridge(args)
            await _close_embedded_bridge(server)
            thread.join(timeout=1.0)

    for _ in range(3):
        await one_cycle()
    async with assert_no_leak_async(
        max_kb=120,
        max_objects=600,
        max_tasks=2,
        label="embed bridge spawn/close wiring",
    ):
        for _ in range(20):
            await one_cycle()
