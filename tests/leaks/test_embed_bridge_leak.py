"""Embedded bridge spawn/close leak pin (mock-only).

The real `pyrustuyabridge.PyBridgeServer` import pulls in a Rust tokio
runtime + ssl + several MB of C heap, and tracemalloc can't see across the
PyO3 boundary, so a real-import N-cycle test would be slow AND blind.

Instead we patch `pyrustuyabridge.PyBridgeServer` to a MagicMock whose
`start()` blocks on a `threading.Event` until `stop()` trips it — that
mirrors the production lifecycle of a single bridge instance per spawn,
so the supervisor's reconfigure-respawn loop does NOT kick in against
the stub (a clean-exit stub would tight-loop until the rate limit). We
measure the Python wiring in `cli._spawn_embedded_bridge` /
`_close_embedded_bridge`: threading.Thread creation, supervisor
construction, MagicMock closure retention, args reference retention.
"""

from __future__ import annotations

import argparse
import threading
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


def _make_blocking_server(**_kw):
    """Stub PyBridgeServer whose start() blocks until stop() is called.

    Each call produces an independent stub with its own Event so the
    supervisor sees exactly one bridge per iteration, matching the
    real lifecycle. Without this the supervisor would respawn rapidly
    against a clean-exit MagicMock and the loop dominates the budget.
    """
    done = threading.Event()
    s = MagicMock()
    s.start.side_effect = lambda: done.wait(timeout=5.0)
    s.stop.side_effect = done.set
    return s


async def test_embed_resolve_cycle_no_leak(tmp_path: Path):
    """N=20 spawn+close cycles (Python wiring only) must stay flat."""
    args = _fake_args(tmp_path)

    async def one_cycle():
        with patch("pyrustuyabridge.PyBridgeServer", side_effect=_make_blocking_server):
            supervisor, thread = _spawn_embedded_bridge(args)
            await _close_embedded_bridge(supervisor)
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
