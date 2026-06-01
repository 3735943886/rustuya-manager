"""State.wait_for / asyncio.Condition waiter leak pins.

asyncio.Condition is supposed to remove a waiter Future from its internal
deque on every wait() exit (success, timeout, or cancellation). If a future
refactor of `State` introduces a new wait path that bypasses the proper
exit (e.g. catches CancelledError without re-raising at the right level),
waiters could accumulate silently.

These are belt-and-suspenders pins — Condition itself is well-tested in
CPython, but the rc19 incident showed that the application code wrapped
around it can defeat the cleanup. Pin both the tracemalloc delta AND the
internal deque length so a regression is caught from either angle.
"""

from __future__ import annotations

import asyncio

from rustuya_manager.state import State

from .conftest import assert_no_leak_async


async def test_wait_for_timeout_no_leak():
    """N=200 timeout cycles on State.wait_for must not accumulate waiters."""
    state = State()
    for _ in range(3):
        await state.wait_for(lambda: False, 0.001)
    async with assert_no_leak_async(
        max_kb=40,
        max_objects=800,
        max_tasks=0,
        label="State.wait_for timeout",
    ):
        # max_objects=800 covers asyncio Future/Handle free-list noise from
        # 200 timeout cycles. The real signal is max_kb=40: a regression
        # that retained waiters would grow tracemalloc into the hundreds
        # of KB, well above this budget.
        for _ in range(200):
            await state.wait_for(lambda: False, 0.001)
    waiters = getattr(state._changed, "_waiters", None)
    if waiters is not None:
        assert len(waiters) == 0, (
            f"Condition._waiters grew to {len(waiters)} after 200 timeout cycles"
        )


async def test_wait_for_concurrent_cancel_no_leak():
    """5 concurrent wait_for tasks, all cancelled, repeated N=30 times.

    Stresses the cancellation cleanup path — multiple Futures need to be
    pulled from the same Condition deque in arbitrary order. A bug here
    would leave dangling Future objects accumulating on every cycle.
    """
    state = State()

    async def waiter():
        await state.wait_for(lambda: False, 5.0)

    async def one_cycle():
        tasks = [asyncio.create_task(waiter()) for _ in range(5)]
        # Yield once so the tasks actually enter wait_for and register on
        # the Condition before we cancel them.
        await asyncio.sleep(0)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for _ in range(3):
        await one_cycle()
    async with assert_no_leak_async(
        max_kb=60,
        max_objects=800,
        max_tasks=0,
        label="State.wait_for concurrent cancel",
    ):
        # max_objects=800 absorbs py3.10 asyncio free-list residue (py3.12
        # sits at ~150, py3.10 at ~406). Real leak detection rides on
        # max_kb=60 + max_tasks=0 (real waiter accumulation would push
        # tracemalloc into the hundreds of KB, not 12 KB).
        for _ in range(30):
            await one_cycle()
    waiters = getattr(state._changed, "_waiters", None)
    if waiters is not None:
        assert len(waiters) == 0, (
            f"Condition._waiters grew to {len(waiters)} after concurrent-cancel cycles"
        )
