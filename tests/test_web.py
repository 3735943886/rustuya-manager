"""Smoke tests for the FastAPI web layer.

Uses FastAPI's `TestClient` (no real broker, no real bridge) so we can verify
the HTTP/WS surface contract independently of the MQTT pipeline. The
BridgeClient is constructed without entering its async context — we wire its
internal `_client` to an aiomqtt mock and pre-flag `_connected` so
`publish_command` runs without actually hitting a broker.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
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
    aiomqtt_mock = MagicMock()
    aiomqtt_mock.publish = AsyncMock(return_value=None)
    aiomqtt_mock.subscribe = AsyncMock(return_value=None)
    aiomqtt_mock.unsubscribe = AsyncMock(return_value=None)
    client._client = aiomqtt_mock
    client._connected.set()
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

    def test_api_state_reports_external_bridge_mode_by_default(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            body = tc.get("/api/state").json()
            assert body["bridge_mode"] == "external"
            assert body["embed_requested"] is False

    def test_api_state_reports_embedded_bridge_mode(self):
        state, client = _fixture_state()
        state.embed_requested = True
        state.bridge_embedded = True
        with TestClient(build_app(state, client)) as tc:
            body = tc.get("/api/state").json()
            assert body["bridge_mode"] == "embedded"
            assert body["embed_requested"] is True

    def test_api_state_surfaces_embed_external_conflict(self):
        # --embed-bridge requested but an external bridge owned the root, so the
        # embed was aborted: mode stays external while embed_requested is True.
        # The client derives the conflict from this pair (+ the warning).
        state, client = _fixture_state()
        state.embed_requested = True
        state.bridge_embedded = False
        with TestClient(build_app(state, client)) as tc:
            body = tc.get("/api/state").json()
            assert body["bridge_mode"] == "external"
            assert body["embed_requested"] is True

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
            # The mock aiomqtt client should have been awaited
            client._client.publish.assert_awaited_once()
            topic, payload = client._client.publish.await_args.args[:2]
            assert topic == "rustuya/command"
            assert json.loads(payload) == {"action": "status", "id": "bridge"}


class TestScanEndpoint:
    """The header's Scan button posts to /api/scan, which delegates to the
    shared LanScanCoordinator. The wire contract:
      - 200 with {ok, count} on success
      - 503 when the broker is down (publish_command raises RuntimeError)
      - exactly one `scan` command published per call (single-flight is
        the coordinator's job; we just verify the endpoint hands off to
        it cleanly)
    """

    def test_api_scan_returns_count_and_publishes_once(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            # Drive the drain to completion immediately by feeding the
            # bridge's empty end-marker the moment the coordinator
            # subscribes. We hook the BridgeClient's scanner subscriber
            # list directly — the coordinator owns the queue.
            orig = client.subscribe_scanner

            def hook():
                q = orig()
                q.put_nowait({"id": "lan-dev", "ip": "10.0.0.5"})
                q.put_nowait({})  # end-marker
                return q

            client.subscribe_scanner = hook  # type: ignore[method-assign]
            r = tc.post("/api/scan")
            assert r.status_code == 200
            assert r.json() == {"ok": True, "count": 1}
            client._client.publish.assert_awaited_once()
            topic, _ = client._client.publish.await_args.args[:2]
            assert topic == "rustuya/command"

    def test_api_scan_returns_503_when_broker_disconnected(self):
        state, client = _fixture_state()
        # publish_command refuses when not connected
        client._connected.clear()
        with TestClient(build_app(state, client)) as tc:
            r = tc.post("/api/scan")
            assert r.status_code == 503


class TestWebSocket:
    def test_ws_sends_initial_snapshot_on_connect(self):
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            with tc.websocket_connect("/ws") as ws:
                msg = ws.receive_json()
                assert msg["version"] == state.version
                assert msg["templates"]["command"] == "rustuya/command"

    def test_ws_handler_races_wait_for_change_against_receive(self):
        """The /ws handler must race `state.wait_for_change` against
        `ws.receive()` so a client disconnect aborts the handler promptly.

        Without the race, each closed client left its server-side handler
        task parked indefinitely on `wait_for_change`, retaining the
        WebSocket object, frame locals, and a slot in the Condition's
        waiter deque. Across browser refreshes that retention grew
        linearly at ~160 KB per cycle.

        TestClient's sync WS model doesn't reliably propagate the
        server-initiated close on this race, so we verify the mechanism
        structurally by reading the handler's source. The fix is also
        exercised end-to-end by tests/e2e_ui (every Playwright test
        opens, uses, and closes a real WS) which would hang or grow
        unbounded if the race were removed.
        """
        import inspect

        state, client = _fixture_state()
        app = build_app(state, client)
        for r in app.routes:
            if getattr(r, "path", "") == "/ws":
                src = inspect.getsource(r.endpoint)
                assert "ws.receive" in src, (
                    "ws_state must race wait_for_change against ws.receive() to "
                    "detect client disconnect; otherwise handler tasks leak per "
                    "connection cycle"
                )
                assert "asyncio.wait" in src, (
                    "ws_state must use asyncio.wait(..., FIRST_COMPLETED) to race "
                    "the state-change wait against client receive"
                )
                return
        pytest.fail("/ws route not registered")

    def test_ws_open_close_cycle_does_not_block(self):
        """Sanity check: opening + closing a /ws connection completes
        cleanly. If a future change broke the race by, e.g., never
        cancelling the change_task, the TestClient teardown could hang.
        """
        state, client = _fixture_state()
        with TestClient(build_app(state, client)) as tc:
            for _ in range(3):
                with tc.websocket_connect("/ws") as ws:
                    ws.receive_json()


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
