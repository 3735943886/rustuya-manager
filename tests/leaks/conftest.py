"""Shared helpers for leak regression tests.

Each test verifies a lifecycle surface doesn't accumulate memory, gc objects,
or asyncio tasks across N repetitions. Pattern:

    # warmup (caches settle, lazy imports happen)
    for _ in range(3):
        do_one_cycle()
    # measurement window
    with assert_no_leak(max_kb=80, max_objects=300, label="..."):
        for _ in range(200):
            do_one_cycle()

The helpers do NOT loop themselves — the caller owns the loop. Helper only
takes snapshots at entry/exit, computes deltas, and fails on budget breach
with a top-N tracemalloc diagnostic.

History motivating these pins (memory: rc18→rc20):
  - rc19 (accaeb0): WS handler leaked ~165 KB/cycle via Condition._waiters
    when wait_for_change wasn't raced against ws.receive().
  - rc20 (43dd389): wizard.close() not called → 2× requests.Session leak,
    ~750-950 KB/cycle on the Pi.
  - rc20 (f5e0065): asyncio.gather cancel race surfaced flaky CancelledError
    in the WS handler cleanup path.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import tracemalloc
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import BridgeTemplates, State

_TOP_N = 10
_TRACE_DEPTH = 25


class AsyncNoop:
    """Awaitable counter that doesn't retain call args (unlike AsyncMock).

    AsyncMock retains every call's args in `call_args_list` and per-call
    bookkeeping in its `_mock_children` graph — over N=200 cycles that
    accumulates hundreds of KB and thousands of gc objects, masking real
    leak signals. AsyncNoop has the same await interface but only tracks
    a single int. Use anywhere a leak test loops over an aiomqtt operation.
    """

    __slots__ = ("await_count",)

    def __init__(self) -> None:
        self.await_count = 0

    async def __call__(self, *args, **kwargs) -> None:
        self.await_count += 1


@dataclass
class _LeakBudget:
    max_kb: float
    max_objects: int
    max_tasks: int


@dataclass
class _Snapshot:
    snap: tracemalloc.Snapshot
    gc_count: int
    task_count: int


def _take_snapshot() -> _Snapshot:
    # Two passes catch cyclic garbage that the first pass freed but couldn't
    # finalize because finalizers ran out of order.
    gc.collect()
    gc.collect()
    snap = tracemalloc.take_snapshot()
    gc_count = len(gc.get_objects())
    try:
        task_count = len(asyncio.all_tasks())
    except RuntimeError:
        task_count = 0
    return _Snapshot(snap=snap, gc_count=gc_count, task_count=task_count)


def _format_diagnostic(label: str, before: _Snapshot, after: _Snapshot, budget: _LeakBudget) -> str:
    diff = after.snap.compare_to(before.snap, "lineno")
    total_kb = sum(s.size_diff for s in diff) / 1024.0
    gc_delta = after.gc_count - before.gc_count
    task_delta = after.task_count - before.task_count
    lines = [
        f"LEAK in {label!r}:",
        f"  tracemalloc: +{total_kb:.1f} KB  (budget {budget.max_kb} KB)",
        f"  gc objects:  +{gc_delta}  (budget {budget.max_objects})",
        f"  asyncio tasks: +{task_delta}  (budget {budget.max_tasks})",
        f"  top {_TOP_N} allocation sites:",
    ]
    shown = 0
    for s in diff:
        if s.size_diff <= 0:
            continue
        lines.append(f"    +{s.size_diff / 1024:.1f} KB  {s.traceback}")
        shown += 1
        if shown >= _TOP_N:
            break
    return "\n".join(lines)


def _check_budget(label: str, before: _Snapshot, after: _Snapshot, budget: _LeakBudget) -> None:
    diff = after.snap.compare_to(before.snap, "lineno")
    total_kb = sum(s.size_diff for s in diff) / 1024.0
    gc_delta = after.gc_count - before.gc_count
    task_delta = after.task_count - before.task_count
    if total_kb > budget.max_kb or gc_delta > budget.max_objects or task_delta > budget.max_tasks:
        pytest.fail(_format_diagnostic(label, before, after, budget))


@contextlib.contextmanager
def assert_no_leak(
    *,
    max_kb: float,
    max_objects: int,
    max_tasks: int = 2,
    label: str = "",
) -> Iterator[None]:
    """Sync contextmanager for TestClient + WebSocket cycles."""
    budget = _LeakBudget(max_kb=max_kb, max_objects=max_objects, max_tasks=max_tasks)
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    tracemalloc.start(_TRACE_DEPTH)
    before = _take_snapshot()
    try:
        yield
    finally:
        after = _take_snapshot()
        tracemalloc.stop()
        _check_budget(label, before, after, budget)


@contextlib.asynccontextmanager
async def assert_no_leak_async(
    *,
    max_kb: float,
    max_objects: int,
    max_tasks: int = 2,
    label: str = "",
) -> AsyncIterator[None]:
    """Async equivalent of assert_no_leak."""
    budget = _LeakBudget(max_kb=max_kb, max_objects=max_objects, max_tasks=max_tasks)
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    tracemalloc.start(_TRACE_DEPTH)
    before = _take_snapshot()
    try:
        yield
    finally:
        after = _take_snapshot()
        tracemalloc.stop()
        _check_budget(label, before, after, budget)


@pytest.fixture(autouse=True)
def _leak_test_isolation():
    """Drop tracemalloc + gc state between leak tests so order can't taint deltas."""
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    gc.collect()
    gc.collect()
    yield
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    gc.collect()
    gc.collect()


def make_ws_fixture() -> tuple[State, BridgeClient]:
    """Build (state, client) wired for build_app() — mirrors test_web.py
    `_fixture_state`. BridgeClient is built without entering its async context;
    `_client` is mocked and `_connected` is pre-set so publish_command runs."""
    state = State()
    state.templates = BridgeTemplates(
        root="rustuya",
        command="rustuya/command",
        event="rustuya/event/{type}/{id}",
        message="rustuya/{level}/{id}",
        scanner="rustuya/scanner",
        payload="{value}",
    )
    client = BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)
    aio = MagicMock()
    aio.publish = AsyncNoop()
    aio.subscribe = AsyncNoop()
    aio.unsubscribe = AsyncNoop()
    client._client = aio
    client._connected.set()
    return state, client


def make_mqtt_fixture(state: State | None = None) -> tuple[BridgeClient, MagicMock]:
    """Build a BridgeClient with aiomqtt mocked — mirrors test_mqtt_client.py
    `_make_client`. Templates pre-seeded so publish_command works."""
    state = state or State()
    if state.templates is None:
        state.templates = BridgeTemplates(
            root="rustuya",
            command="rustuya/cmd/{action}",
            event="rustuya/event/{id}",
            message="rustuya/{level}/{id}",
            scanner="rustuya/scanner",
            payload="{value}",
        )
    client = BridgeClient(
        broker="mqtt://localhost:1883",
        root="rustuya",
        state=state,
    )
    aio = MagicMock()
    aio.subscribe = AsyncNoop()
    aio.unsubscribe = AsyncNoop()
    aio.publish = AsyncNoop()
    client._client = aio
    client._connected.set()
    return client, aio


def make_mock_wizard(
    *,
    login_returns: bool = True,
    fetch_returns: list | None = None,
) -> MagicMock:
    """Mock TuyaWizard suitable for patching `rustuya_manager.wizard.TuyaWizard`.
    Mirrors test_wizard.py `_make_mock_wizard` but skips the QR branch (saved-creds
    happy path) so cycles run fast. `close()` is auto-mocked so leak tests can
    assert `mock_wizard.close.call_count == N` as a strict regression pin for
    rc20 43dd389 (finally block removal would break the count immediately)."""
    fetch_returns = (
        fetch_returns
        if fetch_returns is not None
        else [
            {"id": "bf-x", "name": "lamp", "local_key": "k1", "ip": "192.168.1.10"},
        ]
    )
    mock = MagicMock()
    mock.info = {}

    def login_auto(user_code, creds, qr_callback):
        return login_returns

    mock.login_auto.side_effect = login_auto
    mock.fetch_devices.return_value = fetch_returns
    return mock
