"""Online "is there a newer version?" check for the Info panel.

Both the manager and the embedded bridge are pip-installed, so this compares the
running version against the highest release on PyPI and lets the UI flag
"update available". It is informational only — the manager never self-updates
(it's launched a dozen ways: pip, pipx, systemd, docker, a dev checkout…) and
can't update an external bridge; it just surfaces that a newer build exists.

Two wrinkles this module exists to handle:

  * PyPI's ``info.version`` is the latest *stable* release and omits
    prereleases. These projects are rc-tagged all the way (e.g. ``0.3.0rc25``),
    so we scan the full ``releases`` map ourselves and take the PEP440 max.
  * The bridge's pip wheel (``pyrustuyabridge``) and the version it publishes
    into ``{root}/bridge/config`` are the *same* number now — the wheel was
    re-tagged ``0.3.0rc25`` to match the bridge crate's ``CARGO_PKG_VERSION``.
    So comparing the config-reported bridge version against the wheel's PyPI
    releases is apples-to-apples (``packaging`` normalises the Rust
    ``0.3.0-rc.25`` and PEP440 ``0.3.0rc25`` to the same Version).

Everything here is best-effort: any network/parse failure yields ``None`` and
the caller simply doesn't show a badge. Blocking (urllib) — call ``fetch_latest``
off the event loop.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# PyPI distributions we track. Keys are the snapshot fields the UI reads.
MANAGER_DIST = "rustuya-manager"
BRIDGE_DIST = "pyrustuyabridge"

# Both indexes are consulted and the overall max wins. The two packages live in
# different places under the current release routing: the manager's rc tags
# publish to TestPyPI (release.yml sends only final X.Y.Z to prod PyPI), while
# pyrustuyabridge's rc builds are on prod PyPI. Unioning the indexes gets the
# right answer for both without hard-coding a per-package home, and self-corrects
# if the manager later graduates a release to prod. A stale lower version sitting
# on the "wrong" index can't mislead — only the max across both is taken.
PYPI_INDEXES = ("https://pypi.org/pypi", "https://test.pypi.org/pypi")

# Network bounds. The JSON metadata is small; the cap is a sanity bound against
# a pathological response, not a real size target. The timeout keeps a stalled
# PyPI from holding the background task open.
PYPI_TIMEOUT_S = 10
MAX_PYPI_BYTES = 5 * 1024 * 1024

# How long a fetched result stays good — once a day. Generous on purpose: this
# is an informational nicety, not something worth hammering PyPI for. The disk
# cache also lets a restart show the badge instantly without waiting on the
# network.
CACHE_TTL_S = 24 * 60 * 60

# Cached result in the managed plugin dir, dot-prefixed so plugin discovery
# skips it (same convention as the catalog cache / ledger).
CACHE_NAME = ".version-check.json"


def _index_best(base: str, dist: str) -> Version | None:
    """Highest installable version of ``dist`` on one index (``base``), or
    ``None`` on any network / JSON / shape problem or an absent project.

    Scans the full ``releases`` map rather than trusting ``info.version`` (which
    drops prereleases). Releases whose every file is yanked are skipped — they
    aren't installable, so they shouldn't drive an "update available" prompt."""
    url = f"{base}/{dist}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "rustuya-manager"})
    try:
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT_S) as resp:  # noqa: S310 - https PyPI, fixed hosts
            raw = resp.read(MAX_PYPI_BYTES + 1)
        if len(raw) > MAX_PYPI_BYTES:
            logger.debug("pypi metadata for %s at %s exceeds size bound; skipping", dist, base)
            return None
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("pypi version check for %s at %s failed: %s", dist, base, exc)
        return None
    releases = doc.get("releases") if isinstance(doc, dict) else None
    if not isinstance(releases, dict):
        return None
    best: Version | None = None
    for key, files in releases.items():
        # A release with files, all of them yanked, is not installable.
        if isinstance(files, list) and files and all(f.get("yanked") for f in files):
            continue
        try:
            ver = Version(key)
        except InvalidVersion:
            continue
        if best is None or ver > best:
            best = ver
    return best


def pypi_latest(dist: str) -> str | None:
    """Highest version of ``dist`` across all `PYPI_INDEXES`, prereleases
    included, or ``None`` when no index yields a parseable release. The two
    indexes are unioned (see `PYPI_INDEXES`): the package's real releases live on
    one of them, and a stale lower version on the other can't win the max."""
    best: Version | None = None
    for base in PYPI_INDEXES:
        found = _index_best(base, dist)
        if found is not None and (best is None or found > best):
            best = found
    return str(best) if best is not None else None


def normalize(raw: str | None) -> str | None:
    """Render a version in canonical PEP440 form for display, so the bridge's
    Rust ``CARGO_PKG_VERSION`` ("0.3.0-rc.26") and the manager's PEP440 string
    ("0.1.0rc63") read the same in the UI — same scheme, no stray hyphen/dot.
    Returns the input unchanged if it isn't parseable (don't hide an odd value)."""
    if not raw:
        return raw
    try:
        return str(Version(raw))
    except InvalidVersion:
        return raw


def is_newer(installed: str | None, latest: str | None) -> bool:
    """True iff ``latest`` is a strictly higher version than ``installed``.

    Returns False on any missing or unparseable input — an update prompt must
    never fire on garbage, and "can't tell" reads as "no update" in the UI."""
    if not installed or not latest:
        return False
    try:
        return Version(latest) > Version(installed)
    except InvalidVersion:
        return False


def fetch_latest() -> dict[str, str | None]:
    """Query PyPI for the manager + bridge latest versions. Blocking — call off
    the event loop (asyncio.to_thread). Per-package failures are independent: a
    miss on one still returns the other."""
    return {
        "manager": pypi_latest(MANAGER_DIST),
        "bridge": pypi_latest(BRIDGE_DIST),
    }


def _cache_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / CACHE_NAME


def read_cache(cache_dir: str | Path | None) -> tuple[dict[str, str | None], float] | None:
    """Return ``(latest, fetched_at)`` from the on-disk cache, or ``None`` when
    absent/unreadable. Never raises — a missing or corrupt cache just means a
    fresh fetch."""
    if cache_dir is None:
        return None
    try:
        doc = json.loads(_cache_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    fetched_at = doc.get("fetched_at")
    latest = doc.get("latest")
    if not isinstance(fetched_at, (int, float)) or not isinstance(latest, dict):
        return None
    return (
        {"manager": latest.get("manager"), "bridge": latest.get("bridge")},
        float(fetched_at),
    )


def write_cache(cache_dir: str | Path, latest: dict[str, str | None], fetched_at: float) -> None:
    """Atomically persist a fetched result to ``.version-check.json``. Best
    effort — a write failure is logged by the caller, not fatal."""
    path = _cache_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "fetched_at": fetched_at, "latest": latest}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def cache_is_fresh(fetched_at: float) -> bool:
    """Whether a cache stamped at ``fetched_at`` is still within the TTL."""
    return (time.time() - fetched_at) < CACHE_TTL_S
