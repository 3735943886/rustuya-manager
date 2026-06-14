"""Fixtures for the Playwright UI smoke tests.

The web app is started in a thread on a random localhost port with a
stub BridgeClient — no real MQTT, no network. Each test points the
browser at the same long-lived server, which keeps the suite fast
(startup cost is paid once per session).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import pytest
import uvicorn

from rustuya_manager.state import State
from rustuya_manager.web import build_app

# Auto-tag every test collected under this directory with `e2e` so the
# project-wide `addopts = -m 'not e2e'` in pyproject.toml excludes them
# from a bare `pytest` run. CI re-enables them via `pytest -m e2e`.
#
# pytest passes the full items list to every conftest's hook regardless
# of where the hook is defined, so we filter by path ourselves — only
# items physically under this directory get the marker.
_E2E_DIR = "e2e_ui"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    e2e = pytest.mark.e2e
    for item in items:
        if _E2E_DIR in item.path.parts:
            item.add_marker(e2e)


class _StubBridgeClient:
    """Minimum BridgeClient surface the web app touches.

    Covers the two server-side entry points the UI exercises:
      - `publish_command` for `/api/command` (per-card actions)
      - `subscribe_scanner`/`unsubscribe_scanner` for the LAN scan
        coordinator that backs the header's Scan button
    Calls are recorded so a test can assert "this button publishes
    this action" without needing a real broker. The WS loop never sees
    state change because State stays untouched, so the initial snapshot
    is the only frame the page gets; that's the right shape for static
    UI checks.
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

    def subscribe_scanner(self) -> asyncio.Queue[dict[str, Any]]:
        # Queue the end-marker eagerly so the coordinator's drain loop
        # exits immediately — the e2e suite cares about wiring, not
        # about timing the bridge's UDP scanner.
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        q.put_nowait({})
        return q

    def unsubscribe_scanner(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        return


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(app: Any) -> tuple[str, uvicorn.Server, threading.Thread]:
    """Start `app` on a random localhost port in a daemon thread; return its URL
    plus the handles needed to stop it. uvicorn flips `server.started` once the
    socket is listening; poll until it's true (10s is generous — a healthy local
    start is sub-second)."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start within 10s")
    return f"http://127.0.0.1:{port}", server, thread


def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def server_url() -> str:
    """URL of a running uvicorn instance with the rustuya-manager app.

    Session-scoped so the browser only needs one cold start across the
    whole e2e_ui suite. Daemon thread + `should_exit` flag keeps the
    teardown clean even if a test raises.
    """
    url, server, thread = _start_server(build_app(State(), _StubBridgeClient()))
    yield url
    _stop_server(server, thread)


@pytest.fixture(scope="session")
def server_url_with_plugin(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Like `server_url`, but with one plugin registered that contributes a
    header menu item via an eager init script (the add_header_init route). Lets
    the e2e suite prove a plugin can add a hamburger item without a tab page."""
    static_dir = tmp_path_factory.mktemp("e2e_plugin_static")
    (static_dir / "init.js").write_text(
        "export function init(ctx) {\n"
        "  ctx.addHeaderAction({\n"
        "    id: 'e2e-plugin-action', iconHtml: '★', labelHtml: 'Plugin action',\n"
        "    onClick: () => { document.title = 'plugin-action-fired'; },\n"
        "  });\n"
        "}\n"
    )

    def register(ctx: Any) -> None:
        ctx.add_header_init("e2eplugin", static_dir=str(static_dir))

    url, server, thread = _start_server(build_app(State(), _StubBridgeClient(), plugins=[register]))
    yield url
    _stop_server(server, thread)
