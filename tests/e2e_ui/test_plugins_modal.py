"""Playwright e2e for the host-owned "Manage plugins" modal.

Two checks:
  - the modal opens from the header menu and lists the catalog;
  - a full install round-trip against a local file:// drop-in zip flips the row
    to installed (with Disable/Uninstall actions) without a restart.

The catalog is monkeypatched to a single fake entry pointing at a zip built in
the test's tmp dir, so the round-trip is hermetic — no network, no real plugin.
"""

from __future__ import annotations

import hashlib
import uuid
import zipfile

import pytest
from playwright.sync_api import expect

from rustuya_manager import catalog
from rustuya_manager.state import State
from rustuya_manager.web import build_app

from .conftest import _start_server, _stop_server, _StubBridgeClient

_REGISTER_SRC = """
from fastapi import APIRouter

def register(ctx):
    r = APIRouter()

    @r.get("/api/{route}/ping")
    async def ping():
        return {{"ok": True}}

    ctx.add_api_router(r)
"""


def _make_zip(tmp_path, pkg, route):
    zpath = tmp_path / f"{pkg}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(f"{pkg}/__init__.py", _REGISTER_SRC.format(route=route))
    data = zpath.read_bytes()
    return zpath.as_uri(), hashlib.sha256(data).hexdigest()


@pytest.fixture()
def plugin_server(tmp_path, monkeypatch):
    pkg = f"e2eplug_{uuid.uuid4().hex[:8]}"
    route = f"rt_{uuid.uuid4().hex[:8]}"
    uri, sha = _make_zip(tmp_path, pkg, route)
    entry = {
        "id": "e2e-fake",
        "name": "E2E Fake Plugin",
        "description": "A throwaway plugin used by the e2e suite.",
        "version": "9.9.9",
        "min_api": 1,
        "url": uri,
        "sha256": sha,
    }
    monkeypatch.setattr(catalog, "load_bundled_catalog", lambda: [entry])
    managed = tmp_path / "managed"
    app = build_app(State(), _StubBridgeClient(), managed_plugin_dir=str(managed))
    url, server, thread = _start_server(app)
    yield url
    _stop_server(server, thread)


def _open_modal(page, url):
    page.goto(url)
    expect(page.locator("#conn-badge")).to_contain_text("live")
    page.click("#actions-menu > summary")
    page.click("#manage-plugins-btn")
    expect(page.locator("#plugins-modal")).to_be_visible()


def test_modal_lists_catalog(page, plugin_server):
    _open_modal(page, plugin_server)
    body = page.locator("#plugins-modal-body")
    expect(body).to_contain_text("E2E Fake Plugin")
    expect(body.get_by_role("button", name="Install")).to_be_visible()


def test_install_round_trip(page, plugin_server):
    _open_modal(page, plugin_server)
    body = page.locator("#plugins-modal-body")
    body.get_by_role("button", name="Install").click()
    # After install the row reflects installed state with lifecycle actions, no
    # restart needed.
    expect(body).to_contain_text("installed")
    expect(body.get_by_role("button", name="Disable")).to_be_visible()
    expect(body.get_by_role("button", name="Uninstall")).to_be_visible()
