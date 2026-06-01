"""WS handler leak regression pins.

rc19 (accaeb0) leaked ~165 KB/cycle through `state._changed._waiters` because
the WS handler's `wait_for_change` wasn't raced against `ws.receive()`. A
closed client left its server task parked, retaining the WS object, frame
locals, and a Future slot in the Condition deque.

rc20 (f5e0065) added an `asyncio.shield(asyncio.gather(...))` around the
race-cancel cleanup; without it, teardown-time cancellation of the handler
surfaced as flaky `CancelledError` in the TestClient threadpool.

These tests reuse the WS open/receive/close cycle as the regression pin —
N=200 cycles should produce a deterministic, near-flat memory profile when
both fixes are present. Reverting either fix should cross the budget.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rustuya_manager.web import build_app

from .conftest import assert_no_leak, make_ws_fixture


def test_ws_open_close_no_leak():
    """N=200 open/receive_initial/close cycles must not accumulate memory.

    Each cycle exercises the rc19 race path: server-side, ws_state sends the
    initial snapshot, creates change_task + recv_task, awaits the race; when
    the TestClient context exits, the WS closes, recv_task completes (likely
    with WebSocketDisconnect), change_task is cancelled, and the rc20 shield
    drains the pending cancellation.

    Without the rc19 race, change_task would stay parked across cycles and
    `state._changed._waiters` would grow by 1 per cycle — at 165 KB/cycle the
    budget of 80 KB total is crossed by the first leaked cycle.
    """
    state, client = make_ws_fixture()
    app = build_app(state, client)
    with TestClient(app) as tc:
        for _ in range(3):
            with tc.websocket_connect("/ws") as ws:
                ws.receive_json()
        with assert_no_leak(
            max_kb=200,
            max_objects=2000,
            max_tasks=2,
            label="ws open/close cycle",
        ):
            for _ in range(200):
                with tc.websocket_connect("/ws") as ws:
                    ws.receive_json()


def test_ws_condition_waiters_drain():
    """Direct assertion that `state._changed._waiters` stays bounded.

    This is a more precise pin than tracemalloc for the specific rc19 leak
    site — every parked WS handler corresponded to exactly one Future stuck
    in the Condition's waiter deque. After each open/close cycle the deque
    should drop back to 0.

    `_waiters` is a CPython 3.10+ asyncio.Condition private attribute (a
    `collections.deque` of Futures). If the attribute name changes in a
    future Python release, this test should be skipped rather than removed —
    the tracemalloc test above still catches the underlying leak.
    """
    state, client = make_ws_fixture()
    app = build_app(state, client)
    waiters = getattr(state._changed, "_waiters", None)
    if waiters is None:
        import pytest

        pytest.skip("asyncio.Condition._waiters not present in this Python build")
    with TestClient(app) as tc:
        for _ in range(3):
            with tc.websocket_connect("/ws") as ws:
                ws.receive_json()
        # Baseline: deque should be empty after warmup cycles drain.
        assert len(waiters) == 0, f"warmup did not drain waiters: {len(waiters)}"
        for _ in range(100):
            with tc.websocket_connect("/ws") as ws:
                ws.receive_json()
        # The deque can transiently contain Futures during a cycle, but after
        # every cycle completes (TestClient context exits) it must drop to 0.
        assert len(waiters) == 0, (
            f"Condition._waiters grew to {len(waiters)} after 100 WS cycles — "
            f"rc19 race regression suspected"
        )
