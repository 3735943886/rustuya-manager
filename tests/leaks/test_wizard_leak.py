"""TuyaWizard close() leak regression pins (rc20 43dd389).

rc20 added a `finally: wizard.close()` block to `WizardManager._run` to close
the two `requests.Session` objects (CustomerApi + LoginControl) that tuyawizard
held open. Without it, every wizard run leaked the underlying urllib3
PoolManager + SSL context — ~750-950 KB/cycle on the Pi.

Test #1 (tracemalloc) verifies WizardManager's own machinery (Task, Lock,
WizardSession dataclass) doesn't accumulate across cycles — the mock_wizard
isn't a real requests.Session so the actual leak vector isn't reproducible
here, but the surrounding Python state must stay flat.

Test #2 (call_count) is the strict regression pin: the rc20 finally block
MUST call `wizard.close()` on every cycle. If anyone removes it the
mock's call_count won't match N and the test fails immediately.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from rustuya_manager.wizard import WizardManager, WizardState

from .conftest import assert_no_leak_async, make_mock_wizard


async def test_wizard_start_close_no_leak(tmp_path: Path):
    """N=100 wizard start→drain cycles must not accumulate WizardManager state.

    The mock TuyaWizard returns immediately from login_auto (no QR), so each
    cycle just exercises the WizardManager Task + Lock + WizardSession path.

    Uses `patch(..., new=lambda)` instead of `patch(..., return_value=mock)`
    so the patch target becomes a plain callable rather than a MagicMock —
    the latter records every (info_file=..., logger=...) call into its
    internal `call_args_list`, accumulating hundreds of KB across N cycles
    and masking real leak signals. `mock_wizard` is also created fresh per
    cycle so its own `close.call_args_list` doesn't accumulate.
    """
    creds = str(tmp_path / "tuyacreds.json")
    wm = WizardManager(creds_path=creds)

    async def one_cycle():
        mock_wizard = make_mock_wizard()
        with (
            patch(
                "rustuya_manager.wizard.TuyaWizard",
                new=lambda *a, **k: mock_wizard,
            ),
            patch(
                "rustuya_manager.wizard.postprocess_devices",
                new=lambda *a, **k: None,
            ),
        ):
            await wm.start()
            await wm._task

    for _ in range(3):
        await one_cycle()
    async with assert_no_leak_async(
        max_kb=120,
        max_objects=800,
        max_tasks=2,
        label="wizard start/close cycle",
    ):
        for _ in range(100):
            await one_cycle()


async def test_wizard_close_called_per_cycle(tmp_path: Path):
    """Strict pin: `wizard.close()` must be invoked once per WizardManager.start().

    This catches the exact rc20 43dd389 regression vector — removing the
    `finally: wizard.close()` block would leave the mock's close.call_count
    at 0 instead of N. Tracemalloc can't see the real requests.Session leak
    (the mock isn't a real Session), so this call-count assertion is the
    only deterministic check for the fix's presence.
    """
    creds = str(tmp_path / "tuyacreds.json")
    wm = WizardManager(creds_path=creds)
    mock_wizard = make_mock_wizard()
    N = 50
    with (
        patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard),
        patch("rustuya_manager.wizard.postprocess_devices"),
    ):
        for _ in range(N):
            await wm.start()
            await wm._task
            assert wm.session.state == WizardState.DONE
    assert mock_wizard.close.call_count == N, (
        f"wizard.close() called {mock_wizard.close.call_count} times in {N} cycles — "
        f"rc20 43dd389 finally block may be missing"
    )
