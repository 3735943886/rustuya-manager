"""Plugin catalog + install ledger for the managed plugin directory.

The manager ships a small curated catalog of installable plugins
(`data/plugins.json`, bundled as package data). The UI reads it, annotated with
what's already installed, and lets the user install/update/uninstall — but it
only ever *picks from this catalog*: there is no arbitrary-URL field. Curation
is the trust anchor (plugins run in-process, unsandboxed), exactly the same
trust level as dropping a package into the plugin dir by hand.

Each catalog entry is a drop-in artifact, not a pip package:

    {
      "id":          "rustuya-homeassistant",   # stable catalog id (== distribution name)
      "name":        "Home Assistant Discovery",
      "description": "…",
      "homepage":    "https://github.com/…",
      "version":     "0.0.1rc3",
      "min_api":     1,                          # refuse if > PLUGIN_API_VERSION
      "url":         "https://…/rustuya_ha-0.0.1rc3-dropin.zip",
      "sha256":      "<hex>"                     # integrity of the downloaded zip
    }

Installs land in the *managed plugin dir* (a writable directory the manager owns
— default next to the cloud file, or `--plugin-dir`). Alongside the dropped
package folders the manager keeps an install ledger, `.registry.json`, recording
what each catalog id put on disk so update/uninstall/enable-disable can act on
it precisely:

    {
      "version": 1,
      "plugins": {
        "rustuya-homeassistant": {
          "version":  "0.0.1rc3",
          "packages": ["rustuya_ha"],     # top-level dirs/modules the zip dropped
          "sha256":   "<hex>",
          "min_api":  1,
          "disabled": false
        }
      }
    }

The leading dot keeps `.registry.json` out of `_discover_dir_plugins` (which
skips names starting with `.`/`_`).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import io
import json
import logging
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ledger filename inside the managed plugin dir. Dot-prefixed so plugin
# discovery skips it.
LEDGER_NAME = ".registry.json"

# Cap on a downloaded artifact. A drop-in plugin is a handful of .py files; this
# is a sanity bound against a wrong/hostile URL streaming gigabytes, not a real
# size target.
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

# How long to wait on the download before giving up.
DOWNLOAD_TIMEOUT_S = 30


class CatalogError(Exception):
    """An install/uninstall failure whose message is safe to surface to the UI
    (bad checksum, unsafe archive, network error, …). The endpoint maps it to a
    4xx with this message; anything else stays an opaque 500."""


def load_bundled_catalog() -> list[dict[str, Any]]:
    """Return the curated catalog shipped with the manager (`data/plugins.json`).

    Read via `importlib.resources` so it works the same from a wheel, an
    editable install, or a zipimport. Any read/parse failure is logged and
    yields an empty catalog — a malformed bundle must never break the UI."""
    try:
        raw = (
            importlib.resources.files("rustuya_manager")
            .joinpath("data/plugins.json")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        logger.exception("bundled plugin catalog missing; serving empty catalog")
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("bundled plugin catalog is not valid JSON; serving empty catalog")
        return []
    plugins = doc.get("plugins", []) if isinstance(doc, dict) else []
    return [p for p in plugins if isinstance(p, dict) and p.get("id")]


def _ledger_path(managed_dir: str | Path) -> Path:
    return Path(managed_dir) / LEDGER_NAME


def read_ledger(managed_dir: str | Path | None) -> dict[str, dict[str, Any]]:
    """Return the install ledger's `plugins` map (`{id: record}`), or `{}`.

    Tolerant by design: a missing dir, missing file, or malformed JSON all read
    as an empty ledger so the catalog endpoint still renders."""
    if not managed_dir:
        return {}
    path = _ledger_path(managed_dir)
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("plugin ledger %s unreadable; treating as empty", path)
        return {}
    plugins = doc.get("plugins", {}) if isinstance(doc, dict) else {}
    return {k: v for k, v in plugins.items() if isinstance(v, dict)}


def write_ledger(managed_dir: str | Path, plugins: dict[str, dict[str, Any]]) -> None:
    """Persist the `plugins` map to `.registry.json` in the managed dir.

    Writes atomically (temp file + replace) so a crash mid-write can't leave a
    truncated ledger that would later read as empty and orphan installs."""
    target = Path(managed_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = _ledger_path(target)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "plugins": plugins}, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def annotate_catalog(
    catalog: list[dict[str, Any]],
    ledger: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return catalog entries decorated with install state from the ledger.

    Adds to each entry: `installed` (bool), `installed_version` (str|None),
    `enabled` (bool — meaningful only when installed), and `update_available`
    (installed but at a different version than the catalog's). The original
    entry dicts are not mutated; a shallow copy carries the extra keys."""
    out: list[dict[str, Any]] = []
    for entry in catalog:
        rec = ledger.get(entry["id"])
        installed = rec is not None
        installed_version = rec.get("version") if rec else None
        out.append(
            {
                **entry,
                "installed": installed,
                "installed_version": installed_version,
                "enabled": (not rec.get("disabled", False)) if rec else False,
                "update_available": installed and installed_version != entry.get("version"),
            }
        )
    return out


# ── install pipeline ─────────────────────────────────────────────────────
def _download(url: str) -> bytes:
    """Fetch `url` into memory, bounded by `MAX_ARTIFACT_BYTES`.

    Restricted to http(s) and file:// (the latter for tests/offline mirrors) so
    a catalog entry can't smuggle a `gopher://`/`ftp://` or other surprising
    urllib handler. Network failures become a `CatalogError` with a clean
    message."""
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https", "file"):
        raise CatalogError(f"unsupported artifact URL scheme: {scheme or '(none)'}")
    req = urllib.request.Request(url, headers={"User-Agent": "rustuya-manager"})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310 - scheme allow-listed above
            # Read one byte past the cap so an oversize body is detected, not
            # silently truncated.
            data = resp.read(MAX_ARTIFACT_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise CatalogError(f"could not download plugin artifact: {exc}") from exc
    if len(data) > MAX_ARTIFACT_BYTES:
        raise CatalogError("plugin artifact exceeds the size limit")
    return data


def _verify_sha256(data: bytes, expected: str | None) -> None:
    """Raise unless `data`'s SHA-256 equals `expected` (hex, case-insensitive).

    A missing/blank expected hash is itself a failure — every catalog entry must
    pin a checksum, since it's the integrity guarantee for an unsandboxed,
    in-process download."""
    if not expected:
        raise CatalogError("catalog entry has no sha256 to verify against")
    actual = hashlib.sha256(data).hexdigest()
    if actual.lower() != expected.lower():
        raise CatalogError("plugin artifact checksum does not match the catalog")


def _unpack_zip(data: bytes, dest: Path) -> list[str]:
    """Extract the zip in `data` into `dest`, returning its top-level entry names.

    Hardened against zip-slip: every member path is resolved and required to
    stay inside `dest` before anything is written (reject absolute paths, `..`
    traversal, and symlink members). The returned top-level names (e.g.
    `["rustuya_ha"]`, plus any `__pycache__`/dist-info) are recorded in the
    ledger so uninstall can remove exactly what was dropped."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    top: set[str] = set()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CatalogError("plugin artifact is not a valid zip") from exc
    with zf:
        for info in zf.infolist():
            name = info.filename
            # Symlink members (Unix mode 0o120000 in the high bits) are refused
            # outright — they're the classic archive escape.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise CatalogError(f"refusing symlink in plugin artifact: {name}")
            target = (dest / name).resolve()
            if target != dest and not str(target).startswith(str(dest) + os.sep):
                raise CatalogError(f"unsafe path in plugin artifact: {name}")
            first = name.split("/", 1)[0]
            if first:
                top.add(first)
        zf.extractall(dest)
    return sorted(top)


def _remove_packages(managed_dir: str | Path, packages: list[str]) -> None:
    """Delete the given top-level package names from `managed_dir`.

    Each name is resolved and required to stay directly inside `managed_dir`
    (no traversal) before removal, so a corrupt ledger can't be coerced into
    deleting files elsewhere. Missing entries are ignored — uninstall is
    idempotent."""
    import shutil

    base = Path(managed_dir).resolve()
    for name in packages:
        target = (base / name).resolve()
        if target.parent != base:
            logger.warning("refusing to remove out-of-tree package path %r", name)
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)


def install_plugin(
    entry: dict[str, Any], managed_dir: str | Path, *, replace: bool = False
) -> dict[str, Any]:
    """Download, verify, and unpack a catalog `entry` into `managed_dir`, then
    record it in the ledger and return the new ledger record.

    With `replace=True` (an update), the previously-recorded package files are
    removed first so stale modules from the old version don't linger; the
    `disabled` flag is preserved across the update. The download + checksum are
    done *before* anything on disk changes, so a failed update leaves the old
    install intact.

    Blocking (network + disk) — call it off the event loop (asyncio.to_thread).
    Raises `CatalogError` on any download/checksum/archive problem, leaving the
    ledger untouched. The caller owns the min_api gate and the
    already-installed / installed checks."""
    data = _download(entry["url"])
    _verify_sha256(data, entry.get("sha256"))
    ledger = read_ledger(managed_dir)
    prev = ledger.get(entry["id"], {})
    if replace and prev.get("packages"):
        _remove_packages(managed_dir, prev["packages"])
    packages = _unpack_zip(data, Path(managed_dir))
    record = {
        "version": entry.get("version"),
        "packages": packages,
        "sha256": entry.get("sha256"),
        "min_api": entry.get("min_api", 1),
        "disabled": prev.get("disabled", False) if replace else False,
    }
    ledger[entry["id"]] = record
    write_ledger(managed_dir, ledger)
    logger.info(
        "%s plugin %r v%s -> %s",
        "updated" if replace else "installed",
        entry["id"],
        record["version"],
        packages,
    )
    return record


def uninstall_plugin(plugin_id: str, managed_dir: str | Path) -> None:
    """Remove a ledger-recorded plugin's files and drop its ledger entry.

    No-op if the id isn't in the ledger. The already-imported module keeps
    running until the process restarts — the endpoint signals `restart_required`
    so the UI can offer it."""
    ledger = read_ledger(managed_dir)
    rec = ledger.pop(plugin_id, None)
    if rec is None:
        return
    _remove_packages(managed_dir, rec.get("packages", []))
    write_ledger(managed_dir, ledger)
    logger.info("uninstalled plugin %r", plugin_id)


def set_disabled(plugin_id: str, managed_dir: str | Path, disabled: bool) -> bool:
    """Flip a ledger-recorded plugin's `disabled` flag; return the new value.

    Discovery skips disabled packages (see plugins._discover_dir_plugins), but
    only at the next scan/restart — a currently-loaded plugin stays live until
    then, so the endpoint reports `restart_required`. Raises `CatalogError` if
    the id isn't installed."""
    ledger = read_ledger(managed_dir)
    if plugin_id not in ledger:
        raise CatalogError(f"{plugin_id} is not installed")
    ledger[plugin_id]["disabled"] = disabled
    write_ledger(managed_dir, ledger)
    return disabled


def disabled_packages(managed_dir: str | Path | None) -> frozenset[str]:
    """Top-level package names belonging to disabled plugins — passed to
    discovery so they're left on disk but not loaded."""
    return frozenset(
        pkg
        for rec in read_ledger(managed_dir).values()
        if rec.get("disabled")
        for pkg in rec.get("packages", [])
    )
