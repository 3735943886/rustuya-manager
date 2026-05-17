"""Tests for State's notification primitives.

`wait_for_change` returns on any version bump, which is too coarse when the
caller actually wants to wait for a specific condition — retained MQTT
messages on a fresh subscription fire many version bumps before the real
event the caller is waiting for. `wait_for` takes a predicate so the caller
declares the semantic condition directly.
"""

from __future__ import annotations

import asyncio

from rustuya_manager.models import Device
from rustuya_manager.state import State


class TestWaitForPredicate:
    async def test_returns_true_immediately_if_predicate_already_holds(self):
        state = State()
        await state.set_bridge({"x": Device(id="x")})
        ok = await state.wait_for(lambda: bool(state.bridge), timeout=0.5)
        assert ok is True

    async def test_returns_false_on_timeout(self):
        state = State()
        ok = await state.wait_for(lambda: bool(state.bridge), timeout=0.05)
        assert ok is False
        assert state.bridge == {}

    async def test_wakes_on_relevant_change(self):
        state = State()

        async def populate_later():
            await asyncio.sleep(0.02)
            await state.set_bridge({"x": Device(id="x")})

        task = asyncio.create_task(populate_later())
        ok = await state.wait_for(lambda: bool(state.bridge), timeout=1.0)
        await task
        assert ok is True

    async def test_skips_unrelated_changes_until_predicate_holds(self):
        """Regression: cli wait_for_change(v) used to wake on the first version
        bump regardless of what changed. With retained-message floods, that bump
        was usually an event, not the bridge status reply the CLI cared about.

        wait_for with a predicate skips those unrelated bumps and only resolves
        when the predicate transitions to True."""
        state = State()

        async def noise_then_real():
            await asyncio.sleep(0.02)
            # Simulate retained-event-driven version bumps that aren't the
            # condition the caller wants.
            await state.merge_dps("retained-dev", {"1": True})
            await asyncio.sleep(0.02)
            await state.set_live_status("retained-dev", "online")
            await asyncio.sleep(0.02)
            # Finally, the real condition arrives.
            await state.set_bridge({"x": Device(id="x")})

        task = asyncio.create_task(noise_then_real())
        ok = await state.wait_for(lambda: bool(state.bridge), timeout=1.0)
        await task
        assert ok is True
        # All three intermediate state changes occurred — but only the third
        # satisfied the predicate.
        assert state.bridge == {"x": Device(id="x")}
        assert "retained-dev" in state.dps  # noise was actually applied too


class TestRemoveDevice:
    async def test_clears_every_per_device_bucket(self):
        state = State()
        await state.set_bridge({"x": Device(id="x"), "keep": Device(id="keep")})
        await state.merge_dps("x", {"1": True})
        await state.set_live_status("x", "online", code=0, message="")
        await state.record_response("x", {"action": "get", "status": "ok"})
        # Sanity: every bucket is populated for x
        assert "x" in state.bridge
        assert "x" in state.dps
        assert "x" in state.live_status
        assert "x" in state.last_seen
        assert "x" in state.last_response

        v0 = state.version
        await state.remove_device("x")

        for bucket in (
            state.bridge,
            state.dps,
            state.live_status,
            state.last_seen,
            state.last_response,
        ):
            assert "x" not in bucket
        # Unrelated entries untouched
        assert "keep" in state.bridge
        # Version bumped exactly once for the whole atomic clear
        assert state.version == v0 + 1

    async def test_unknown_id_is_noop(self):
        state = State()
        await state.set_bridge({"keep": Device(id="keep")})
        v0 = state.version
        await state.remove_device("never-existed")
        assert state.version == v0  # no bump
        assert "keep" in state.bridge


class TestRetainedOnly:
    """Retained MQTT events arrive without a publish timestamp, so `last_seen`
    must NOT be stamped — that would falsely show "just now" for stale data
    that's been sitting on the broker for hours. The id goes into
    `retained_only` instead; UI surfaces this as a "(retained)" label. The
    moment a live event arrives, last_seen takes over and the flag drops."""

    async def test_retained_merge_dps_skips_last_seen(self):
        state = State()
        await state.merge_dps("x", {"1": True}, retained=True)
        assert state.dps["x"] == {"1": True}
        assert "x" not in state.last_seen
        assert "x" in state.retained_only

    async def test_live_event_after_retained_promotes(self):
        # Cold-start path: retained event lands first, then a live one. The
        # live event must clear retained_only and set last_seen so the UI
        # switches from "(retained)" to "X ago".
        state = State()
        await state.merge_dps("x", {"1": True}, retained=True)
        assert "x" in state.retained_only

        await state.merge_dps("x", {"1": False})
        assert "x" not in state.retained_only
        assert "x" in state.last_seen

    async def test_retained_after_live_does_not_downgrade(self):
        # Once a device is "fresh", a later retained event (e.g. broker
        # re-delivering after a reconnect) must not push it back into
        # retained_only and must not clobber the existing last_seen.
        state = State()
        await state.merge_dps("x", {"1": True})  # live first
        live_seen = state.last_seen["x"]

        await state.merge_dps("x", {"1": True}, retained=True)
        assert "x" not in state.retained_only
        assert state.last_seen["x"] == live_seen

    async def test_retained_record_response_skips_last_seen(self):
        state = State()
        await state.record_response("x", {"action": "get", "status": "ok"}, retained=True)
        assert "x" not in state.last_seen
        assert "x" in state.retained_only

    async def test_remove_clears_retained_flag(self):
        # remove_device must drop the retained_only flag too — otherwise a
        # re-added device would still be tagged "retained" indefinitely.
        state = State()
        await state.merge_dps("x", {"1": True}, retained=True)
        assert "x" in state.retained_only
        await state.remove_device("x")
        assert "x" not in state.retained_only
