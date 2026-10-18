"""Wizard changes wake independent consumers without polling or executor-thread writes."""

import asyncio
import threading
from unittest.mock import Mock

import pytest

from rustuya_manager.wizard import WizardManager, WizardState


async def test_waiters_observe_changes_before_and_after_subscription():
    wizard = WizardManager("unused.json")
    first = asyncio.create_task(wizard.wait_state_change(WizardState.IDLE))
    cancelled = asyncio.create_task(wizard.wait_state_change(WizardState.IDLE))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    wizard._update(state=WizardState.AWAITING_SCAN)
    assert (await first).state == WizardState.AWAITING_SCAN
    # A transition before subscription must not be lost.
    assert (await wizard.wait_state_change(WizardState.IDLE)).state == WizardState.AWAITING_SCAN
    assert wizard._changed.is_set() is False


async def test_thread_qr_callback_wakes_waiters_and_late_callbacks_are_ignored(monkeypatch):
    release = threading.Event()
    callbacks = []

    def login_auto(user_code, creds, callback):
        callbacks.append(callback)
        callback("tuyaSmart--qrLogin?token=test")
        release.wait(timeout=5)
        return True

    native = Mock(login_auto=login_auto)
    monkeypatch.setattr("rustuya_manager.wizard.TuyaWizard", lambda **kwargs: native)
    wizard = WizardManager("unused.json")
    await wizard.start()
    try:
        session = await asyncio.wait_for(wizard.wait_state_change(WizardState.REQUESTING_QR), 2)
        assert session.state == WizardState.AWAITING_SCAN
        assert session.qr_image_data_url.startswith("data:image/svg+xml;base64,")
        await wizard.cancel()
        assert wizard.session.state == WizardState.ERROR
        # A blocked executor call can still issue callbacks after cancellation.
        await asyncio.to_thread(callbacks[0], None)
        assert wizard.session.state == WizardState.ERROR
    finally:
        release.set()
        await wizard.cancel()


async def test_constructor_failure_notifies_error(monkeypatch):
    def fail(**kwargs):
        raise ValueError("invalid credentials")

    monkeypatch.setattr("rustuya_manager.wizard.TuyaWizard", fail)
    wizard = WizardManager("unused.json")
    await wizard.start()
    session = await asyncio.wait_for(wizard.wait_state_change(WizardState.REQUESTING_QR), 1)
    assert session.state == WizardState.ERROR
    assert session.error_code == "unexpected"
    await wizard._task
