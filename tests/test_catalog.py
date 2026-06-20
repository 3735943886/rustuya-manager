"""Tests for the plugin catalog + install ledger (Increment A).

Covers the bundled-catalog loader, the on-disk ledger round-trip, install-state
annotation, and the GET /api/plugins/catalog endpoint — including the
zero-plugin / no-managed-dir defaults.
"""

from __future__ import annotations

import hashlib
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from rustuya_manager import catalog
from rustuya_manager.mqtt import BridgeClient
from rustuya_manager.plugins import PLUGIN_API_VERSION
from rustuya_manager.state import State
from rustuya_manager.web import build_app


def _make_client(state: State) -> BridgeClient:
    return BridgeClient(broker="mqtt://localhost:1883", root="rustuya", state=state)


# A drop-in plugin whose register() adds a router with a unique ping route, so a
# test can prove the freshly-installed plugin was actually wired into the app.
_REGISTER_SRC = """
from fastapi import APIRouter

def register(ctx):
    r = APIRouter()

    @r.get("/api/{route}/ping")
    async def ping():
        return {{"ok": True}}

    ctx.add_api_router(r)
"""


def _make_dropin_zip(tmp_path, *, pkg_name, route, extra=None):
    """Build a drop-in zip containing a uniquely-named package and return
    (file_uri, sha256). `extra` is an optional {arcname: bytes} of extra members
    (used to exercise zip-slip rejection)."""
    zpath = tmp_path / f"{pkg_name}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(f"{pkg_name}/__init__.py", _REGISTER_SRC.format(route=route))
        for arc, payload in (extra or {}).items():
            zf.writestr(arc, payload)
    data = zpath.read_bytes()
    return zpath.as_uri(), hashlib.sha256(data).hexdigest()


def _uniq(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ── bundled catalog ──────────────────────────────────────────────────────
def test_bundled_catalog_has_homeassistant_entry():
    entries = catalog.load_bundled_catalog()
    by_id = {e["id"]: e for e in entries}
    assert "rustuya-homeassistant" in by_id
    ha = by_id["rustuya-homeassistant"]
    # Required fields the install pipeline + UI rely on.
    for key in ("name", "description", "version", "min_api", "url", "sha256"):
        assert key in ha, f"catalog entry missing {key!r}"
    assert isinstance(ha["min_api"], int)


# ── ledger round-trip ────────────────────────────────────────────────────
def test_read_ledger_absent_is_empty(tmp_path):
    assert catalog.read_ledger(None) == {}
    assert catalog.read_ledger(tmp_path) == {}  # dir exists, no .registry.json


def test_read_ledger_malformed_is_empty(tmp_path):
    (tmp_path / catalog.LEDGER_NAME).write_text("{not json", encoding="utf-8")
    assert catalog.read_ledger(tmp_path) == {}


def test_write_then_read_ledger(tmp_path):
    rec = {"rustuya-homeassistant": {"version": "0.0.1rc3", "packages": ["rustuya_ha"]}}
    catalog.write_ledger(tmp_path, rec)
    assert catalog.read_ledger(tmp_path) == rec
    # The ledger file is dot-prefixed so plugin discovery skips it.
    assert (tmp_path / catalog.LEDGER_NAME).name.startswith(".")


# ── annotation ───────────────────────────────────────────────────────────
def test_annotate_not_installed():
    cat = [{"id": "x", "version": "1.0"}]
    out = catalog.annotate_catalog(cat, {})
    assert out[0]["installed"] is False
    assert out[0]["installed_version"] is None
    assert out[0]["enabled"] is False
    assert out[0]["update_available"] is False


def test_annotate_installed_same_version():
    cat = [{"id": "x", "version": "1.0"}]
    out = catalog.annotate_catalog(cat, {"x": {"version": "1.0", "disabled": False}})
    assert out[0]["installed"] is True
    assert out[0]["installed_version"] == "1.0"
    assert out[0]["enabled"] is True
    assert out[0]["update_available"] is False


def test_annotate_update_available_and_disabled():
    cat = [{"id": "x", "version": "2.0"}]
    out = catalog.annotate_catalog(cat, {"x": {"version": "1.0", "disabled": True}})
    assert out[0]["installed"] is True
    assert out[0]["update_available"] is True
    assert out[0]["enabled"] is False


def test_annotate_does_not_mutate_input():
    cat = [{"id": "x", "version": "1.0"}]
    catalog.annotate_catalog(cat, {"x": {"version": "1.0"}})
    assert set(cat[0]) == {"id", "version"}


# ── endpoint ─────────────────────────────────────────────────────────────
def test_catalog_endpoint_no_managed_dir():
    state = State()
    with TestClient(build_app(state, _make_client(state))) as tc:
        body = tc.get("/api/plugins/catalog").json()
    assert body["managed"] is False
    assert body["api_version"] == PLUGIN_API_VERSION
    ha = next(p for p in body["plugins"] if p["id"] == "rustuya-homeassistant")
    assert ha["installed"] is False


def test_catalog_endpoint_reflects_ledger(tmp_path):
    catalog.write_ledger(
        tmp_path,
        {"rustuya-homeassistant": {"version": "0.0.1rc3", "disabled": False}},
    )
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        body = tc.get("/api/plugins/catalog").json()
    assert body["managed"] is True
    ha = next(p for p in body["plugins"] if p["id"] == "rustuya-homeassistant")
    assert ha["installed"] is True
    assert ha["installed_version"] == "0.0.1rc3"
    assert ha["enabled"] is True


# ── install: unit (download/verify/unpack) ───────────────────────────────
def test_verify_sha256_mismatch_and_missing():
    with pytest.raises(catalog.CatalogError):
        catalog._verify_sha256(b"data", "deadbeef")
    with pytest.raises(catalog.CatalogError):
        catalog._verify_sha256(b"data", "")


def test_unpack_rejects_zip_slip(tmp_path):
    pkg = _uniq("slip")
    uri, _ = _make_dropin_zip(tmp_path, pkg_name=pkg, route="x", extra={"../escape.py": b"x = 1"})
    data = (tmp_path / f"{pkg}.zip").read_bytes()
    with pytest.raises(catalog.CatalogError, match="unsafe path"):
        catalog._unpack_zip(data, tmp_path / "dest")
    # Nothing escaped.
    assert not (tmp_path / "escape.py").exists()


def test_install_plugin_unit_writes_package_and_ledger(tmp_path):
    pkg = _uniq("fakeplug")
    managed = tmp_path / "managed"
    uri, sha = _make_dropin_zip(tmp_path, pkg_name=pkg, route="u")
    entry = {"id": "fake", "version": "1.2.3", "url": uri, "sha256": sha, "min_api": 1}
    record = catalog.install_plugin(entry, managed)
    assert (managed / pkg / "__init__.py").is_file()
    assert pkg in record["packages"]
    assert record["version"] == "1.2.3"
    assert catalog.read_ledger(managed)["fake"]["version"] == "1.2.3"


def test_install_plugin_unit_checksum_mismatch_leaves_no_ledger(tmp_path):
    pkg = _uniq("fakeplug")
    managed = tmp_path / "managed"
    uri, _ = _make_dropin_zip(tmp_path, pkg_name=pkg, route="m")
    entry = {"id": "fake", "version": "1", "url": uri, "sha256": "00" * 32, "min_api": 1}
    with pytest.raises(catalog.CatalogError, match="checksum"):
        catalog.install_plugin(entry, managed)
    assert catalog.read_ledger(managed) == {}


# ── install: endpoint ────────────────────────────────────────────────────
def _patch_catalog(monkeypatch, entry):
    monkeypatch.setattr(catalog, "load_bundled_catalog", lambda: [entry])


def test_install_endpoint_wires_plugin_live(tmp_path, monkeypatch):
    pkg, route = _uniq("fakeplug"), _uniq("rt")
    managed = tmp_path / "managed"
    uri, sha = _make_dropin_zip(tmp_path, pkg_name=pkg, route=route)
    entry = {
        "id": "fake",
        "name": "Fake",
        "version": "0.1",
        "url": uri,
        "sha256": sha,
        "min_api": 1,
    }
    _patch_catalog(monkeypatch, entry)
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(managed))
    with TestClient(app) as tc:
        resp = tc.post("/api/plugins/install", json={"id": "fake"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["installed_version"] == "0.1"
        # Freshly installed plugin's route is live without a restart.
        assert tc.get(f"/api/{route}/ping").json() == {"ok": True}
        # Catalog now reflects the install.
        cat = tc.get("/api/plugins/catalog").json()
        assert next(p for p in cat["plugins"] if p["id"] == "fake")["installed"] is True


def test_install_endpoint_rejects_when_no_managed_dir(monkeypatch):
    entry = {"id": "fake", "version": "1", "url": "file:///x", "sha256": "x", "min_api": 1}
    _patch_catalog(monkeypatch, entry)
    state = State()
    with TestClient(build_app(state, _make_client(state))) as tc:
        assert tc.post("/api/plugins/install", json={"id": "fake"}).status_code == 400


def test_install_endpoint_unknown_id(tmp_path, monkeypatch):
    _patch_catalog(monkeypatch, {"id": "other", "version": "1"})
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        assert tc.post("/api/plugins/install", json={"id": "fake"}).status_code == 404


def test_install_endpoint_min_api_too_high(tmp_path, monkeypatch):
    entry = {
        "id": "fake",
        "version": "1",
        "url": "file:///x",
        "sha256": "x",
        "min_api": PLUGIN_API_VERSION + 1,
    }
    _patch_catalog(monkeypatch, entry)
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        assert tc.post("/api/plugins/install", json={"id": "fake"}).status_code == 409


def test_install_endpoint_already_installed(tmp_path, monkeypatch):
    catalog.write_ledger(tmp_path, {"fake": {"version": "1"}})
    entry = {"id": "fake", "version": "1", "url": "file:///x", "sha256": "x", "min_api": 1}
    _patch_catalog(monkeypatch, entry)
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        assert tc.post("/api/plugins/install", json={"id": "fake"}).status_code == 409


def test_install_endpoint_checksum_mismatch_returns_400(tmp_path, monkeypatch):
    pkg, route = _uniq("fakeplug"), _uniq("rt")
    uri, _ = _make_dropin_zip(tmp_path, pkg_name=pkg, route=route)
    entry = {"id": "fake", "version": "1", "url": uri, "sha256": "00" * 32, "min_api": 1}
    _patch_catalog(monkeypatch, entry)
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path / "m"))
    with TestClient(app) as tc:
        assert tc.post("/api/plugins/install", json={"id": "fake"}).status_code == 400


# ── lifecycle: update / uninstall / enable-disable ───────────────────────
def test_update_replaces_files_and_preserves_disabled(tmp_path):
    managed = tmp_path / "managed"
    pkg = _uniq("fakeplug")
    uri1, sha1 = _make_dropin_zip(tmp_path, pkg_name=pkg, route="a")
    e1 = {"id": "fake", "version": "1.0", "url": uri1, "sha256": sha1, "min_api": 1}
    catalog.install_plugin(e1, managed)
    catalog.set_disabled("fake", managed, True)
    # New version ships a different top-level package name; the old one must go.
    pkg2 = _uniq("fakeplug2")
    uri2, sha2 = _make_dropin_zip(tmp_path, pkg_name=pkg2, route="b")
    e2 = {"id": "fake", "version": "2.0", "url": uri2, "sha256": sha2, "min_api": 1}
    rec = catalog.install_plugin(e2, managed, replace=True)
    assert rec["version"] == "2.0"
    assert rec["disabled"] is True  # preserved across update
    assert not (managed / pkg).exists()  # old package removed
    assert (managed / pkg2 / "__init__.py").is_file()


def test_uninstall_removes_files_and_ledger(tmp_path):
    managed = tmp_path / "managed"
    pkg = _uniq("fakeplug")
    uri, sha = _make_dropin_zip(tmp_path, pkg_name=pkg, route="a")
    catalog.install_plugin(
        {"id": "fake", "version": "1", "url": uri, "sha256": sha, "min_api": 1}, managed
    )
    catalog.uninstall_plugin("fake", managed)
    assert not (managed / pkg).exists()
    assert catalog.read_ledger(managed) == {}
    catalog.uninstall_plugin("fake", managed)  # idempotent, no raise


def test_disabled_packages_skipped_by_discovery(tmp_path):
    from rustuya_manager.plugins import discover_plugins

    managed = tmp_path / "managed"
    pkg = _uniq("fakeplug")
    uri, sha = _make_dropin_zip(tmp_path, pkg_name=pkg, route="a")
    catalog.install_plugin(
        {"id": "fake", "version": "1", "url": uri, "sha256": sha, "min_api": 1}, managed
    )
    skip = catalog.disabled_packages(managed)
    assert skip == frozenset()  # enabled by default → discovered
    assert len(discover_plugins(plugin_dirs=[str(managed)], skip_packages=skip)) == 1

    catalog.set_disabled("fake", managed, True)
    skip = catalog.disabled_packages(managed)
    assert pkg in skip
    assert discover_plugins(plugin_dirs=[str(managed)], skip_packages=skip) == []


def test_update_endpoint_restart_required(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    pkg = _uniq("fakeplug")
    uri1, sha1 = _make_dropin_zip(tmp_path, pkg_name=pkg, route=_uniq("r"))
    catalog.install_plugin(
        {"id": "fake", "version": "1", "url": uri1, "sha256": sha1, "min_api": 1}, managed
    )
    pkg2 = _uniq("fakeplug2")
    uri2, sha2 = _make_dropin_zip(tmp_path, pkg_name=pkg2, route=_uniq("r"))
    _patch_catalog(
        monkeypatch,
        {"id": "fake", "version": "2", "url": uri2, "sha256": sha2, "min_api": 1},
    )
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(managed))
    with TestClient(app) as tc:
        resp = tc.post("/api/plugins/update", json={"id": "fake"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["restart_required"] is True
        assert body["installed_version"] == "2"


def test_uninstall_and_toggle_endpoints(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    pkg = _uniq("fakeplug")
    uri, sha = _make_dropin_zip(tmp_path, pkg_name=pkg, route=_uniq("r"))
    entry = {"id": "fake", "name": "F", "version": "1", "url": uri, "sha256": sha, "min_api": 1}
    _patch_catalog(monkeypatch, entry)
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(managed))
    with TestClient(app) as tc:
        tc.post("/api/plugins/install", json={"id": "fake"})
        # disable
        r = tc.post("/api/plugins/toggle", json={"id": "fake", "enabled": False})
        assert r.status_code == 200 and r.json()["restart_required"] is True
        assert catalog.read_ledger(managed)["fake"]["disabled"] is True
        cat = tc.get("/api/plugins/catalog").json()
        assert next(p for p in cat["plugins"] if p["id"] == "fake")["enabled"] is False
        # re-enable
        tc.post("/api/plugins/toggle", json={"id": "fake", "enabled": True})
        assert catalog.read_ledger(managed)["fake"]["disabled"] is False
        # uninstall
        r = tc.post("/api/plugins/uninstall", json={"id": "fake"})
        assert r.status_code == 200 and r.json()["restart_required"] is True
        assert catalog.read_ledger(managed) == {}
        # uninstall again → 404
        assert tc.post("/api/plugins/uninstall", json={"id": "fake"}).status_code == 404


def test_toggle_not_installed_404(tmp_path):
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        r = tc.post("/api/plugins/toggle", json={"id": "ghost", "enabled": False})
        assert r.status_code == 404


# ── live (remote) catalog: fetch / cache / refresh endpoint ───────────────
_FAKE_REMOTE = [
    {
        "id": "rustuya-homeassistant",
        "name": "Home Assistant Discovery",
        "version": "9.9.9",
        "min_api": 1,
        "url": "https://example/rustuya_ha-9.9.9-dropin.zip",
        "sha256": "00",
    }
]


def test_effective_catalog_bundled_when_no_cache(tmp_path):
    eff, source, checked_at = catalog.effective_catalog(tmp_path)
    assert source == "bundled"
    assert checked_at is None
    assert eff == catalog.load_bundled_catalog()


def test_refresh_catalog_caches_and_effective_reads_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "fetch_remote_catalog", lambda: list(_FAKE_REMOTE))
    entries, ts = catalog.refresh_catalog(tmp_path)
    assert entries == _FAKE_REMOTE
    assert isinstance(ts, float)
    # cache persisted, dot-prefixed so plugin discovery skips it
    assert (tmp_path / catalog.CATALOG_CACHE_NAME).is_file()
    assert catalog.CATALOG_CACHE_NAME.startswith(".")
    # effective now serves the cached remote catalog with its fetch time
    eff, source, checked_at = catalog.effective_catalog(tmp_path)
    assert source == "remote"
    assert eff == _FAKE_REMOTE
    assert checked_at == ts


def test_refresh_catalog_failure_raises_and_falls_back(tmp_path, monkeypatch):
    def boom():
        raise catalog.CatalogError("network down")

    monkeypatch.setattr(catalog, "fetch_remote_catalog", boom)
    with pytest.raises(catalog.CatalogError):
        catalog.refresh_catalog(tmp_path)
    # nothing cached → effective stays bundled
    _eff, source, checked_at = catalog.effective_catalog(tmp_path)
    assert source == "bundled"
    assert checked_at is None


def test_get_catalog_includes_source_and_checked_at(tmp_path):
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        body = tc.get("/api/plugins/catalog").json()
        assert body["source"] == "bundled"
        assert body["checked_at"] is None


def test_refresh_endpoint_success_then_get_serves_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "fetch_remote_catalog", lambda: list(_FAKE_REMOTE))
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        body = tc.post("/api/plugins/catalog/refresh").json()
        assert body["ok"] is True
        assert body["error"] is None
        assert body["source"] == "remote"
        ha = next(p for p in body["plugins"] if p["id"] == "rustuya-homeassistant")
        assert ha["version"] == "9.9.9"
        # the cached remote catalog now drives the plain GET too
        g = tc.get("/api/plugins/catalog").json()
        assert g["source"] == "remote"
        assert any(p["version"] == "9.9.9" for p in g["plugins"])


def test_refresh_endpoint_failure_falls_back_to_bundled(tmp_path, monkeypatch):
    def boom():
        raise catalog.CatalogError("offline")

    monkeypatch.setattr(catalog, "fetch_remote_catalog", boom)
    state = State()
    app = build_app(state, _make_client(state), managed_plugin_dir=str(tmp_path))
    with TestClient(app) as tc:
        body = tc.post("/api/plugins/catalog/refresh").json()
        assert body["ok"] is False
        assert "offline" in body["error"]
        assert body["source"] == "bundled"
        # still serves a usable catalog so the panel keeps rendering
        assert any(p["id"] == "rustuya-homeassistant" for p in body["plugins"])
