"""Inverse parser for the bridge's payload templates.

Given a payload template (with `{var}` placeholders) and a concrete payload
string the bridge produced from it, recover the variable values.

The trick is a sentinel substitution + JSON tree walk:

1. Replace every `{var}` in the template with a unique string sentinel,
   wrapping in quotes so the result is valid JSON regardless of whether
   the original placeholder was bare or already quoted.
2. JSON-parse both the sentinel-template and the incoming payload.
3. Walk the two trees in parallel; whenever the template element is a
   sentinel string, capture the payload element at that position
   (whatever its type) under the corresponding variable name.

This handles any JSON-shaped template — nested objects, arrays, mixed types
— because the JSON parser does the structural heavy lifting. The only
failure modes are:

- Non-JSON templates (e.g. `value={value};ts={timestamp}` text style).
  The sentinelled template fails to JSON-parse, and `parse_payload_with_template`
  returns None. Callers should fall through to the bridge's generic
  parse_mqtt_payload or surface a warning.
- A payload whose structure doesn't match the template
  (different keys, wrong array length, mismatched literals). Returns None.

For the warning UX, `validate_payload_template` answers "would the manager
be able to extract values from anything the bridge generates with this
template?" — useful at bootstrap to nudge users toward shapes the manager
knows how to read.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The bridge's `replace_vars` substitutes these. Restricting our regex to
# the same set means we don't accidentally rewrite literal `{anything}`
# text the user put in the template by mistake.
KNOWN_VARS = frozenset(
    {"id", "name", "cid", "dp", "type", "level", "value", "dps", "timestamp", "root", "action"}
)

# Match either a quoted placeholder "{var}" or a bare {var}.
# Either form is replaced with `"<sentinel>"` so the result is always a
# JSON string at parse time.
_PLACEHOLDER_RE = re.compile(
    r'"\{(' + "|".join(KNOWN_VARS) + r')\}"|\{(' + "|".join(KNOWN_VARS) + r")\}"
)


def _substitute_sentinels(template: str) -> tuple[str, dict[str, str]]:
    """Returns (sentinelled_template, {sentinel_str: var_name})."""
    sentinels: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        var = match.group(1) or match.group(2)
        sent = f"__RM_S_{len(sentinels)}__"
        sentinels[sent] = var
        return f'"{sent}"'

    return _PLACEHOLDER_RE.sub(replace, template), sentinels


def _walk(template: Any, payload: Any, sentinels: dict[str, str], out: dict[str, Any]) -> bool:
    """Recursively match template against payload; capture sentinel positions."""
    if isinstance(template, str) and template in sentinels:
        out[sentinels[template]] = payload
        return True
    if isinstance(template, dict) and isinstance(payload, dict):
        if set(template.keys()) != set(payload.keys()):
            return False
        for k in template:
            if not _walk(template[k], payload[k], sentinels, out):
                return False
        return True
    if isinstance(template, list) and isinstance(payload, list):
        if len(template) != len(payload):
            return False
        return all(_walk(t, p, sentinels, out) for t, p in zip(template, payload, strict=True))
    # Primitive — must match exactly (sentinels already short-circuited above).
    return template == payload


def parse_payload_with_template(payload: str, template: str) -> dict[str, Any] | None:
    """Extract every `{var}`'s value from a concrete payload.

    Returns a dict mapping var name → captured value, or None when the
    template+payload can't be matched (non-JSON template, structure
    mismatch, no placeholders to capture)."""
    sentinelled, sentinels = _substitute_sentinels(template)
    if not sentinels:
        return None
    try:
        parsed_template = json.loads(sentinelled)
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError:
        return None
    captures: dict[str, Any] = {}
    if not _walk(parsed_template, parsed_payload, sentinels, captures):
        return None
    return captures


def validate_payload_template(template: str | None) -> tuple[bool, str]:
    """Answer 'can the manager extract values from payloads built with this
    template?'.

    Returns (ok, human_message). When `ok` is False, the message explains
    what's wrong and how to fix it at the bridge config level."""
    if not template:
        return False, "No payload template received from bridge."
    sentinelled, sentinels = _substitute_sentinels(template)
    if not sentinels:
        return False, (
            f"Payload template '{template}' has no recognized placeholders. "
            "Manager can't extract DPS values from events. "
            "Update mqtt_payload_template in the bridge config to a JSON shape "
            "containing at least one of {value}, {dps}, {timestamp}."
        )
    try:
        json.loads(sentinelled)
    except json.JSONDecodeError as e:
        return False, (
            f"Payload template '{template}' isn't valid JSON after placeholder "
            f"substitution ({e.msg}). Manager can't extract DPS values from events. "
            "Use a JSON shape — e.g. '{value}' (default), '{\"v\":{value}}', "
            '\'{dps}\', or \'{"id":"{id}","data":{dps}}\' — in the bridge\'s '
            "mqtt_payload_template."
        )
    return True, "Template recognized."
