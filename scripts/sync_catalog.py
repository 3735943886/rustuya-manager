#!/usr/bin/env python3
"""Daily catalog-sync bot: bump curated plugin entries to their newest release.

The manager's live catalog is `src/rustuya_manager/data/plugins.json`, and each
entry pins three things that change every time a tracked plugin cuts a release:
`version`, the release-asset `url`, and the artifact `sha256`. Keeping those in
sync has been manual (see the `chore(catalog): point … to rc<N>` commits). This
script does it deterministically:

  1. Read the tracking manifest `data/plugin-sources.json` ({id: {repo, asset}}).
  2. For each tracked catalog entry, ask GitHub for the newest release —
     PRERELEASES INCLUDED, since the plugins ship `rc` builds and the plain
     `/releases/latest` endpoint hides those. Draft releases are skipped.
  3. If the release version differs from the catalog entry, download the
     matching asset, compute its sha256, and rewrite version/url/sha256.
  4. Write the catalog back only when something changed, and emit a markdown
     summary (for the PR body) plus a machine-readable `changed=` line.

Stdlib only — same dependency-free posture as catalog.py, so it runs on a bare
`actions/setup-python` step with no install. Exit status:
  0  success (whether or not anything changed; check the `changed=` line)
  1  a tracked plugin could not be resolved (network/asset/parse) — the job
     fails loudly rather than silently skipping a plugin forever.

Auth: set GITHUB_TOKEN in the environment to raise the API rate limit and read
private repos; unset, it falls back to anonymous requests (fine for low volume).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Catalog + manifest live side by side in the package data dir. Resolve relative
# to this file so the script works from any CWD (cron runners checkout to a
# random path).
DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rustuya_manager" / "data"
CATALOG_PATH = DATA_DIR / "plugins.json"
SOURCES_PATH = DATA_DIR / "plugin-sources.json"

# Mirror catalog.py's download bound — a drop-in plugin is a handful of .py
# files; this guards against a wrong/hostile asset streaming gigabytes.
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
HTTP_TIMEOUT_S = 30


def _gh_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "rustuya-catalog-sync",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 - github api, https
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "rustuya-catalog-sync"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 - release asset, https
        data = resp.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"asset exceeds {MAX_ARTIFACT_BYTES} byte limit")
    return data


def latest_release(repo: str) -> dict[str, Any]:
    """Newest published release for `owner/repo`, prereleases included.

    `/releases` is ordered newest-first by creation; `/releases/latest` is
    avoided because it excludes prereleases (and the tracked plugins are all on
    `rc` builds). The first non-draft entry is the one we want — drafts have no
    public assets yet."""
    releases = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=10")
    for rel in releases:
        if not rel.get("draft"):
            return rel
    raise ValueError(f"{repo}: no published (non-draft) release found")


def _version_from_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def _find_asset(release: dict[str, Any], wanted_name: str) -> dict[str, Any]:
    """Pick the release asset to publish. Prefer an exact name match against the
    manifest pattern; fall back to a sole `*-dropin.zip` so a one-off naming
    drift doesn't wedge the sync, but refuse to guess between several."""
    assets = release.get("assets", [])
    for asset in assets:
        if asset.get("name") == wanted_name:
            return asset
    dropins = [a for a in assets if str(a.get("name", "")).endswith("-dropin.zip")]
    if len(dropins) == 1:
        return dropins[0]
    raise ValueError(
        f"could not resolve asset {wanted_name!r} in release "
        f"{release.get('tag_name')} (assets: {[a.get('name') for a in assets]})"
    )


def main() -> int:
    catalog_doc = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8")).get("sources", {})

    entries = {e["id"]: e for e in catalog_doc.get("plugins", []) if e.get("id")}
    changes: list[str] = []
    errors: list[str] = []

    for plugin_id, src in sources.items():
        entry = entries.get(plugin_id)
        if entry is None:
            errors.append(f"`{plugin_id}` is in plugin-sources.json but not in plugins.json")
            continue
        repo = src["repo"]
        try:
            release = latest_release(repo)
            new_version = _version_from_tag(release["tag_name"])
            if new_version == entry.get("version"):
                print(f"= {plugin_id}: up to date at {new_version}")
                continue
            asset = _find_asset(release, src["asset"].format(version=new_version))
            data = _download(asset["browser_download_url"])
            digest = hashlib.sha256(data).hexdigest()
        except (urllib.error.URLError, KeyError, ValueError, OSError) as exc:
            errors.append(f"`{plugin_id}` ({repo}): {exc}")
            print(f"! {plugin_id}: {exc}", file=sys.stderr)
            continue

        old_version = entry.get("version")
        entry["version"] = new_version
        entry["url"] = asset["browser_download_url"]
        entry["sha256"] = digest
        changes.append(f"- **{entry.get('name', plugin_id)}**: `{old_version}` → `{new_version}`")
        print(f"+ {plugin_id}: {old_version} -> {new_version}")

    if changes:
        # Match write_catalog_cache's formatting: 2-space indent + trailing
        # newline, so the diff is just the changed fields.
        CATALOG_PATH.write_text(json.dumps(catalog_doc, indent=2) + "\n", encoding="utf-8")

    body_lines = ["## Catalog sync", ""]
    body_lines += changes if changes else ["No plugin updates available."]
    if errors:
        body_lines += ["", "### ⚠️ Unresolved", *(f"- {e}" for e in errors)]
    body = "\n".join(body_lines) + "\n"
    Path("catalog-sync-body.md").write_text(body, encoding="utf-8")

    # Machine-readable signals for the workflow (peter-evans only opens a PR when
    # the tree actually changed, but this lets the step summary read cleanly).
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changes else 'false'}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body)

    # Errors fail the job (after writing any clean changes) so a broken tracked
    # plugin is visible, not silently stale.
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
