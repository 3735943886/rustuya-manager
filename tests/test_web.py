"""Smoke tests for the FastAPI web layer.

Uses FastAPI's `TestClient` (no real broker, no real bridge) so we can verify
the HTTP/WS surface contract independently of the MQTT pipeline. The
BridgeClient is constructed but its `run()` is never invoked — only
`publish_command` is reachable, and we test it via a mock paho client.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
from fastapi.testclient import TestClient

from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.state import BridgeTemplates, State
from rustuya_manager.web import build_app


def _fixture_state() -> tuple[State, BridgeClient]:
    state = State()
    # Pre-seed templates so /api/command can render the publish topic
    state.templates = BridgeTemplates(
        root="rustuya",
        command="rustuya/command",
        event="rustuya/event/{type}/{id}",
        message="rustuya/{level}/{id}",
        scanner="rustuya/scanner",
        payload="{value}",
    )
    client = BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)
    paho_mock = MagicMock(spec=mqtt.Client)
    paho_mock.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    client._client = paho_mock
    return state, client


class TestHTTP:
    def test_root_serves_html(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            r = tc.get("/")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/html")
            assert "<title>rustuya-manager</title>" in r.text
            # Tailwind is pulled from CDN — no build step is the explicit goal
            assert "cdn.tailwindcss.com" in r.text
            assert "/static/app.js" in r.text

    def test_static_app_js_served(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            r = tc.get("/static/app.js")
            assert r.status_code == 200
            # Sanity: it's the ES-module entry point, not the HTML page
            assert "import" in r.text
            assert "./ws.js" in r.text

    def test_static_modules_served(self):
        # Every ES module the entry imports must be reachable on /static/*.
        # Catches missing files and bad package-data globs in pyproject.
        state, client = _fixture_state()
        modules = {
            "state.js": "expandedIds",
            "dom.js": "escapeHtml",
            "api.js": "publishCommand",
            "ws.js": "WebSocket",
            "cards.js": "deviceCard",
            "render.js": "renderDevices",
            "modal-sync.js": "openSyncModal",
            "modal-wizard.js": "applyWizardSession",
            "modal-device.js": "openAddModal",
            "modal-confirm.js": "initConfirmModal",
        }
        with TestClient(build_app(state, client)) as tc:
            for name, marker in modules.items():
                r = tc.get(f"/static/{name}")
                assert r.status_code == 200, f"{name} not served"
                assert marker in r.text, f"{name} missing expected marker {marker!r}"

    def test_api_state_returns_full_snapshot(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            r = tc.get("/api/state")
            assert r.status_code == 200
            body = r.json()
            assert "version" in body
            assert body["templates"]["root"] == "rustuya"
            assert "diff" in body and "synced" in body["diff"]

    def test_api_command_validates_action(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/command", json={})
            assert r.status_code == 400

    def test_api_command_publishes(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/command", json={"action": "status", "id": "bridge"})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            # The mock paho client should have been called
            client._client.publish.assert_called_once()
            topic, payload = client._client.publish.call_args.args[:2]
            assert topic == "rustuya/command"
            assert json.loads(payload) == {"action": "status", "id": "bridge"}


class TestWebSocket:
    def test_ws_sends_initial_snapshot_on_connect(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            with tc.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
                assert msg["version"] == state.version
                assert msg["templates"]["command"] == "rustuya/command"


class TestBasicAuth:
    """The auth middleware lives at the ASGI level so a single credential
    pair gates both the HTTP surface AND the WebSocket handshake. Tests
    confirm both, plus that omitting --auth keeps the app fully open."""

    @staticmethod
    def _make_app(auth: str | None):
        state, client = _fixture_state()
        return build_app(state, client, auth=auth)

    def test_no_auth_means_no_gate(self):
        with TestClient(self._make_app(None)) as tc:
            assert tc.get("/api/state").status_code == 200

    def test_missing_credentials_returns_401(self):
        with TestClient(self._make_app("admin:secret")) as tc:
            r = tc.get("/api/state")
            assert r.status_code == 401
            # WWW-Authenticate must be present so browsers prompt for creds.
            assert r.headers.get("www-authenticate", "").lower().startswith("basic")

    def test_correct_credentials_pass_through(self):
        with TestClient(self._make_app("admin:secret")) as tc:
            r = tc.get("/api/state", auth=("admin", "secret"))
            assert r.status_code == 200

    def test_wrong_credentials_return_401(self):
        with TestClient(self._make_app("admin:secret")) as tc:
            r = tc.get("/api/state", auth=("admin", "wrong"))
            assert r.status_code == 401

    def test_websocket_rejects_without_credentials(self):
        # TestClient surfaces a closed-before-accept upgrade as an exception
        # of varying concrete type depending on starlette/httpx versions; we
        # only care that the WS handshake did NOT complete.
        import pytest

        with TestClient(self._make_app("admin:secret")) as tc:
            with pytest.raises(Exception):  # noqa: B017 - upgrade-rejection shape varies
                with tc.websocket_connect("/ws"):
                    pass

    def test_websocket_accepts_with_credentials(self):
        # Basic auth on WS via httpx TestClient is set on the underlying
        # transport — we encode the credential header explicitly because
        # websocket_connect doesn't take an auth kwarg.
        import base64

        token = base64.b64encode(b"admin:secret").decode()
        with TestClient(self._make_app("admin:secret")) as tc:
            with tc.websocket_connect("/ws", headers={"Authorization": f"Basic {token}"}) as ws:
                msg = ws.receive_json()
                assert "version" in msg

    def test_malformed_auth_arg_raises(self):
        import pytest

        with pytest.raises(ValueError, match="user:password"):
            self._make_app("missing-colon")
