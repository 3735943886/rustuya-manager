"""Helpers for loading/saving the Tuya Cloud device JSON (`tuyadevices.json`).

Both the CLI and the web upload path go through this module. The file format
accepts either a top-level list of devices or a dict-of-devices keyed by id —
the same shapes that `tuyawizard` emits.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import Device


class CloudFormatError(ValueError):
    """Raised when the uploaded JSON doesn't look like a Tuya devices file."""


def parse_cloud_json(raw: str | bytes) -> dict[str, Device]:
    """Parses raw JSON text into a {id: Device} map. Raises CloudFormatError on
    malformed input."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CloudFormatError(f"invalid JSON: {e}") from None

    iterable: list[dict[str, Any]]
    if isinstance(data, list):
        iterable = data
    elif isinstance(data, dict):
        iterable = list(data.values())
    else:
        raise CloudFormatError("expected a list or dict at the top level")

    devices: dict[str, Device] = {}
    for entry in iterable:
        if not isinstance(entry, dict):
            continue
        if "id" not in entry:
            continue
        devices[entry["id"]] = Device.from_dict(entry)

    if not devices:
        raise CloudFormatError("no devices with an 'id' field found")
    return devices


def load_cloud_file(path: Path) -> dict[str, Device]:
    """Reads a Tuya devices JSON file from disk."""
    return parse_cloud_json(path.read_bytes())


def save_cloud_json(raw: str | bytes, path: Path) -> None:
    """Writes the JSON to disk atomically (temp file + rename). The raw input
    is validated first to avoid persisting broken files."""
    parse_cloud_json(raw)  # validate

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    # Pretty-print for human inspection — same shape, just formatted.
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        pass  # already validated above; fall back to raw text

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tuyadevices.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except Exception:
        # On any failure, drop the temp file so we don't leave debris.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
