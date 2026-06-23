"""Unit tests for the Info panel's online version check (versions.py).

Pure logic + a mocked PyPI fetch — no network. Covers the two things this
module exists to get right: picking the PEP440 max *including prereleases*
(PyPI's info.version drops them, and these projects are rc-tagged), and never
firing an update prompt on missing/unparseable/equal versions.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from rustuya_manager import versions


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_pypi(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    return patch.object(versions.urllib.request, "urlopen", return_value=_FakeResp(body))


def test_is_newer_basic_ordering():
    assert versions.is_newer("0.1.0rc61", "0.1.0rc62") is True
    assert versions.is_newer("0.1.0rc62", "0.1.0rc61") is False  # downgrade
    assert versions.is_newer("0.3.0rc25", "0.3.0rc25") is False  # equal


def test_is_newer_normalises_rust_and_pep440():
    # The bridge publishes CARGO_PKG_VERSION ("0.3.0-rc.25"); the wheel/PyPI use
    # PEP440 ("0.3.0rc25"). packaging treats them as equal — no false update.
    assert versions.is_newer("0.3.0-rc.25", "0.3.0rc25") is False


def test_is_newer_guards_missing_and_garbage():
    assert versions.is_newer(None, "0.3.0rc25") is False
    assert versions.is_newer("0.3.0rc25", None) is False
    assert versions.is_newer("not-a-version", "0.3.0rc25") is False
    assert versions.is_newer("0.3.0rc25", "garbage") is False


def test_pypi_latest_picks_max_including_prereleases():
    # info.version is the stable 0.1.4, but the real max is the rc — the scan
    # must beat info.version.
    payload = {
        "info": {"version": "0.1.4"},
        "releases": {
            "0.1.4": [{"yanked": False}],
            "0.2.0rc25": [{"yanked": False}],
            "0.3.0rc25": [{"yanked": False}],
        },
    }
    with _patch_pypi(payload):
        assert versions.pypi_latest("pyrustuyabridge") == "0.3.0rc25"


def test_pypi_latest_skips_fully_yanked_release():
    payload = {
        "info": {"version": "0.1.4"},
        "releases": {
            "0.2.0rc25": [{"yanked": False}],
            "0.3.0rc25": [{"yanked": True}, {"yanked": True}],  # all files yanked
        },
    }
    with _patch_pypi(payload):
        assert versions.pypi_latest("pyrustuyabridge") == "0.2.0rc25"


def test_pypi_latest_ignores_unparseable_keys():
    payload = {"releases": {"weird-tag": [{"yanked": False}], "0.2.0rc25": [{"yanked": False}]}}
    with _patch_pypi(payload):
        assert versions.pypi_latest("pyrustuyabridge") == "0.2.0rc25"


def test_pypi_latest_returns_none_on_network_error():
    with patch.object(versions.urllib.request, "urlopen", side_effect=OSError("offline")):
        assert versions.pypi_latest("rustuya-manager") is None


def test_cache_roundtrip(tmp_path):
    latest = {"manager": "0.1.0rc62", "bridge": "0.3.0rc25"}
    stamp = time.time()
    versions.write_cache(tmp_path, latest, stamp)
    got = versions.read_cache(tmp_path)
    assert got is not None
    data, fetched_at = got
    assert data == latest
    assert abs(fetched_at - stamp) < 1
    assert versions.cache_is_fresh(fetched_at) is True
    assert versions.cache_is_fresh(stamp - versions.CACHE_TTL_S - 10) is False


def test_read_cache_absent_or_corrupt(tmp_path):
    assert versions.read_cache(None) is None
    assert versions.read_cache(tmp_path) is None  # no file yet
    (tmp_path / versions.CACHE_NAME).write_text("{ not json", encoding="utf-8")
    assert versions.read_cache(tmp_path) is None
