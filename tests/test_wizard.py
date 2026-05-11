"""Tests for the WizardManager async wrapper + /api/wizard/* endpoints.

`tuyawizard.TuyaWizard` is the upstream sync class — we mock it out so the
test never touches the real Tuya cloud. The point of these tests is to verify
our state machine and HTTP surface, not the upstream SDK.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest
from fastapi.testclient import TestClient

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import BridgeTemplates, State
from rustuya_manager.web import build_app
from rustuya_manager.wizard import WizardManager, WizardState


SAMPLE_DEVICES = [
    {"id": "bf-aaaa", "name": "lamp", "local_key": "k1", "ip": "192.168.1.10"},
    {"id": "bf-bbbb", "name": "switch", "local_key": "k2", "ip": "192.168.1.11"},
]


def _make_mock_wizard(*, get_qr_returns=("token123", "tuyaSmart--qrLogin?token=abc"),
                     wait_returns=True, fetch_returns=None):
    """Returns a mock TuyaWizard with the methods our wrapper calls."""
    fetch_returns = fetch_returns if fetch_returns is not None else SAMPLE_DEVICES
    mock = MagicMock()
    mock.info = {}
    mock.get_qr_url.return_value = get_qr_returns
    mock.wait_for_login_result.return_value = wait_returns
    mock.init_manager.return_value = None
    mock.fetch_devices.return_value = fetch_returns
    return mock


# ─────────────────────────────────────────────────────────────────────────────
# WizardManager — direct tests with TuyaWizard mocked
# ─────────────────────────────────────────────────────────────────────────────

class TestWizardManager:
    async def test_happy_path_calls_callback(self, tmp_path: Path):
        received: list[list[dict]] = []

        async def on_devices(devices):
            received.append(devices)

        creds = str(tmp_path / "tuyacreds.json")
        wm = WizardManager(
            creds_path=creds,
            on_devices=on_devices,
            postprocess_mode="",  # skip — postprocess scan would touch the network
        )
        mock_wizard = _make_mock_wizard()

        with patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard):
            session = await wm.start()
            assert session.state == WizardState.REQUESTING_QR
            # Drain the background task
            await wm._task

        assert wm.session.state == WizardState.DONE
        assert wm.session.devices_count == 2
        assert received == [SAMPLE_DEVICES]
        # QR image was generated for awaiting_scan
        assert wm.session.qr_image_data_url is not None
        assert wm.session.qr_image_data_url.startswith("data:image/svg+xml;base64,")

    async def test_timeout_yields_error_state(self, tmp_path: Path):
        wm = WizardManager(creds_path=str(tmp_path / "creds.json"), postprocess_mode="")
        mock_wizard = _make_mock_wizard(wait_returns=False)
        with patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard):
            await wm.start()
            await wm._task
        assert wm.session.state == WizardState.ERROR
        assert "not completed" in (wm.session.error or "").lower()

    async def test_exception_in_fetch_yields_error(self, tmp_path: Path):
        wm = WizardManager(creds_path=str(tmp_path / "creds.json"), postprocess_mode="")
        mock_wizard = _make_mock_wizard()
        mock_wizard.fetch_devices.side_effect = RuntimeError("API down")
        with patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard):
            await wm.start()
            await wm._task
        assert wm.session.state == WizardState.ERROR
        assert "API down" in (wm.session.error or "")

    async def test_double_start_returns_running_session(self, tmp_path: Path):
        """A second start() while one is still running must not spawn a 2nd
        task. Otherwise we'd race on tuyacreds.json."""
        wm = WizardManager(creds_path=str(tmp_path / "creds.json"), postprocess_mode="")
        mock_wizard = _make_mock_wizard()
        # Make wait_for_login_result sleep so the task is still running when
        # we call start() the second time.
        import time
        def slow_wait(*args, **kwargs):
            time.sleep(0.3)
            return True
        mock_wizard.wait_for_login_result.side_effect = slow_wait
        with patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard):
            await wm.start()
            first_task = wm._task
            await asyncio.sleep(0.05)
            await wm.start()  # should be a no-op
            assert wm._task is first_task
            await wm._task

    async def test_postprocess_invoked_when_mode_set(self, tmp_path: Path):
        wm = WizardManager(
            creds_path=str(tmp_path / "creds.json"),
            postprocess_mode="parent",
        )
        mock_wizard = _make_mock_wizard()
        with patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard), \
             patch("rustuya_manager.wizard.postprocess_devices") as pp:
            await wm.start()
            await wm._task
        pp.assert_called_once()
        assert pp.call_args.args[1] == "parent"


# ─────────────────────────────────────────────────────────────────────────────
# /api/wizard/* endpoints (full app, sync TestClient)
# ─────────────────────────────────────────────────────────────────────────────

def _build_app_fixture(tmp_path: Path):
    state = State()
    state.templates = BridgeTemplates(
        root="rustuya",
        command="rustuya/command",
        event="rustuya/event/{type}/{id}",
        message="rustuya/{level}/{id}",
        scanner="rustuya/scanner",
        payload="{value}",
    )
    state.cloud_path = str(tmp_path / "tuyadevices.json")
    client = BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)
    paho_mock = MagicMock(spec=mqtt.Client)
    paho_mock.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    client._client = paho_mock
    return state, client, str(tmp_path / "tuyacreds.json")


class TestWizardEndpoints:
    def test_status_idle_when_never_started(self, tmp_path: Path):
        state, client, creds = _build_app_fixture(tmp_path)
        with TestClient(build_app(state, client, creds_path=creds)) as tc:
            r = tc.get("/api/wizard/status")
            assert r.status_code == 200
            body = r.json()
            assert body["state"] == "idle"

    def test_start_then_status_reaches_done(self, tmp_path: Path):
        """End-to-end: POST /start → poll /status → verify devices loaded."""
        state, client, creds = _build_app_fixture(tmp_path)
        mock_wizard = _make_mock_wizard()
        with patch("rustuya_manager.wizard.TuyaWizard", return_value=mock_wizard), \
             TestClient(build_app(state, client, creds_path=creds)) as tc:
            # Bypass postprocess scan (no network in tests)
            tc.app.state.wizard.postprocess_mode = ""

            r = tc.post("/api/wizard/start", json={"user_code": "TEST123"})
            assert r.status_code == 200
            # Poll until we converge to done or error
            import time
            for _ in range(40):
                body = tc.get("/api/wizard/status").json()
                if body["state"] in ("done", "error"):
                    break
                time.sleep(0.1)
            assert body["state"] == "done", body
            assert body["devices_count"] == 2

        # Devices populated into state.cloud + persisted to disk
        assert set(state.cloud) == {"bf-aaaa", "bf-bbbb"}
        assert Path(state.cloud_path).exists()
