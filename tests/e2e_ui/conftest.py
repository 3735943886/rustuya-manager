"""Fixtures for the Playwright UI smoke tests.

The web app is started in a thread on a random localhost port with a
stub BridgeClient — no real MQTT, no network. Each test points the
browser at the same long-lived server, which keeps the suite fast
(startup cost is paid once per session).
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest
import uvicorn

from rustuya_manager.state import State
from rustuya_manager.web import build_app


class _StubBridgeClient:
    """Minimum BridgeClient surface the web app touches.

    `/api/command` calls publish_command — we just record the call so a
    test can assert "this button publishes this action" without needing
    a broker. The WS loop never sees state change because State stays
    untouched, so the initial snapshot is the only frame the page gets;
    that's the right shape for static UI checks.
    """

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_command(
        self,
        action: str,
        target_id: str | None = None,
        target_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.published.append(
            {"action": action, "id": target_id, "name": target_name, "extra": extra}
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url() -> str:
    """URL of a running uvicorn instance with the rustuya-manager app.

    Session-scoped so the browser only needs one cold start across the
    whole e2e_ui suite. Daemon thread + `should_exit` flag keeps the
    teardown clean even if a test raises.
    """
    state = State()
    app = build_app(state, _StubBridgeClient())

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # uvicorn flips `server.started` once the socket is listening; poll
    # until it's true (or bail out). 10s is generous — a healthy local
    # start is sub-second.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start within 10s")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
