"""Plugin-declared topic/retain requirements and their evaluation.

A plugin depends on the bridge's topic scheme carrying (or not carrying) certain
placeholders — e.g. a plugin that maps events per-DP needs `{dp}` in the event
topic, and one that addresses devices needs `{id}`. Today that dependency is
implicit: misconfigure the bridge and the plugin silently misbehaves. This
module lets a plugin *declare* its needs (`ctx.require_topic` / `ctx.require_retain`,
see `plugins.py`); the manager evaluates them against the live bridge config,
surfaces any gap in the Info panel, and offers a guided fix (the operator edits
the proposed template and the manager pushes it with the bridge's `set_config`).

Everything here is a pure function over data so it can run on every config change
(re-evaluated in `web.serialize_state`) and be unit-tested without a broker.

Design rules that keep a merged requirement set always satisfiable — so no
combination of plugins can deadlock the recommendation:

  * **Retain is "requires True" only.** A plugin can ask for `mqtt_retain=True`;
    it cannot ask for `False`. The merge is then a plain OR (any plugin needs it
    → recommend True), which can never conflict with another plugin.
  * **Present beats absent.** When one plugin needs a placeholder present and
    another needs it absent, *present wins* — adding a placeholder a plugin
    ignores is harmless, removing one a plugin needs breaks its routing. The
    plugin whose `must_not_have` lost is reported as *not honored* (not green),
    so the override is truthful rather than hidden.
  * **Bridge minimums are never removed.** Each topic's default-scheme
    placeholders (`TEMPLATE_SPECS[...].default`) are treated as the bridge's
    routing minimum and are protected from any `must_not_have` — the binding
    doesn't expose per-topic minimums, so the known-good defaults stand in.

Precedence, therefore: bridge-minimum present > any `must_have` present >
`must_not_have` absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The four topic templates a plugin may constrain, with the bridge config key
# that carries each, the bridge's default (whose placeholders are the protected
# routing minimum), and the placeholders the bridge understands in that topic.
# `payload` is intentionally absent — its extractability is already validated by
# the bridge binding (`mqtt._validate_payload_template`), a separate concern.


@dataclass(frozen=True)
class TemplateSpec:
    config_key: str
    default: str
    allowed: frozenset[str]


TEMPLATE_SPECS: dict[str, TemplateSpec] = {
    "command": TemplateSpec(
        "mqtt_command_topic", "{root}/command", frozenset({"root", "id", "name", "action"})
    ),
    "event": TemplateSpec(
        "mqtt_event_topic",
        "{root}/event/{type}/{id}",
        frozenset({"root", "type", "id", "name", "dp"}),
    ),
    "message": TemplateSpec(
        "mqtt_message_topic", "{root}/{level}/{id}", frozenset({"root", "level", "id", "name"})
    ),
    "scanner": TemplateSpec("mqtt_scanner_topic", "{root}/scanner", frozenset({"root"})),
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass(frozen=True)
class TopicRequirement:
    """One plugin's placeholder constraints on one topic template."""

    source: str  # plugin-supplied label, for UI attribution
    template: str  # a key of TEMPLATE_SPECS
    must_have: tuple[str, ...] = ()
    must_not_have: tuple[str, ...] = ()


def placeholders(template: str | None) -> set[str]:
    """The set of `{placeholder}` names in a template string (empty for None)."""
    return set(_PLACEHOLDER_RE.findall(template or ""))


def validate_requirement(
    template: str, must_have: tuple[str, ...], must_not_have: tuple[str, ...]
) -> None:
    """Raise ValueError if a declared requirement can't ever be meaningful — an
    unknown template, an unknown placeholder for that template, a placeholder
    asked to be both present and absent, or a bridge-minimum asked to be absent.
    Called at declaration time so a plugin author sees the mistake immediately."""
    spec = TEMPLATE_SPECS.get(template)
    if spec is None:
        raise ValueError(f"unknown template {template!r}; expected one of {sorted(TEMPLATE_SPECS)}")
    mh, mnh = set(must_have), set(must_not_have)
    unknown = (mh | mnh) - spec.allowed
    if unknown:
        raise ValueError(
            f"unknown placeholder(s) {sorted(unknown)} for {template!r} topic; allowed: {sorted(spec.allowed)}"
        )
    both = mh & mnh
    if both:
        raise ValueError(
            f"placeholder(s) {sorted(both)} declared both must_have and must_not_have for {template!r}"
        )
    protected = placeholders(spec.default)
    blocked = mnh & protected
    if blocked:
        raise ValueError(
            f"cannot require {sorted(blocked)} absent from {template!r} topic — the bridge needs them for routing"
        )


def validate_topic_value(template: str, value: str) -> tuple[bool, str]:
    """Pre-flight a candidate topic template before it's pushed to the bridge via
    `set_config`. Returns `(ok, message)`. Catches the mistakes the manager can
    judge locally — unknown template key, empty value, an MQTT wildcard (`+`/`#`,
    which `set_config` also rejects but a clear message beats a bridge NACK), or
    a placeholder the bridge doesn't understand in that topic. The bridge remains
    the final authority on deeper validity."""
    spec = TEMPLATE_SPECS.get(template)
    if spec is None:
        return False, f"unknown template {template!r}"
    if not value or not value.strip():
        return False, "topic must not be empty"
    if "+" in value or "#" in value:
        return False, "topic must not contain MQTT wildcards (+ or #)"
    unknown = placeholders(value) - spec.allowed
    if unknown:
        return False, f"unknown placeholder(s) {sorted(unknown)} for {template!r} topic"
    return True, "ok"


def propose_template(current: str, *, add: set[str], remove: set[str]) -> str:
    """Best-effort corrected template: drop standalone `{x}` segments to remove,
    append `/{x}` segments to add. Deliberately conservative — it only touches
    segments that are exactly one placeholder, so it never corrupts a mixed
    segment like `dp/{dp}/state`. The result is a *suggestion* the operator
    edits, not an authoritative rewrite."""
    segments = current.split("/")
    kept = [
        s for s in segments if not (s.startswith("{") and s.endswith("}") and s[1:-1] in remove)
    ]
    for name in sorted(add):
        kept.append(f"{{{name}}}")
    return "/".join(kept)


def evaluate(
    raw_config: dict[str, Any] | None,
    topic_reqs: list[TopicRequirement],
    retain_required_by: list[str],
) -> dict[str, Any] | None:
    """Evaluate all plugin requirements against the live bridge config.

    Returns a JSON-serializable report for the snapshot, or None when no plugin
    declared anything (so a plugin-less build's wire format is unchanged). The
    report carries, per constrained topic, the current and recommended template,
    what's missing/forbidden, and per-source status (incl. `unhonored` entries
    where present-wins overrode a `must_not_have`); plus the retain verdict.
    """
    if not topic_reqs and not retain_required_by:
        return None

    cfg = raw_config or {}
    by_template: dict[str, list[TopicRequirement]] = {}
    for req in topic_reqs:
        by_template.setdefault(req.template, []).append(req)

    topics: dict[str, Any] = {}
    overall = True
    for tkey, reqs in by_template.items():
        spec = TEMPLATE_SPECS[tkey]
        current = cfg.get(spec.config_key) or spec.default
        cur_ph = placeholders(current)

        must_have_union: set[str] = set()
        for r in reqs:
            must_have_union |= set(r.must_have)
        # present-wins + bridge-minimum: a must_not_have only takes effect if no
        # one needs it present and it isn't a routing minimum.
        protected = placeholders(spec.default) | must_have_union
        must_not_union: set[str] = set()
        for r in reqs:
            must_not_union |= set(r.must_not_have)
        effective_forbidden = must_not_union - protected

        missing = sorted(must_have_union - cur_ph)
        forbidden_present = sorted(effective_forbidden & cur_ph)
        satisfied = not missing and not forbidden_present
        overall = overall and satisfied

        sources = []
        for r in reqs:
            r_missing = sorted(set(r.must_have) - cur_ph)
            # placeholders this source wanted absent but were kept (present-wins)
            r_unhonored = sorted(set(r.must_not_have) & protected)
            r_forbidden_present = sorted((set(r.must_not_have) - protected) & cur_ph)
            sources.append(
                {
                    "source": r.source,
                    "must_have": list(r.must_have),
                    "must_not_have": list(r.must_not_have),
                    "satisfied": not r_missing and not r_forbidden_present,
                    "unhonored": r_unhonored,
                }
            )

        topics[tkey] = {
            "config_key": spec.config_key,
            "current": current,
            "recommended": propose_template(
                current, add=set(missing), remove=set(forbidden_present)
            ),
            "satisfied": satisfied,
            "missing": missing,
            "forbidden": forbidden_present,
            "sources": sources,
        }

    retain = None
    if retain_required_by:
        current_retain = bool(cfg.get("mqtt_retain", False))
        retain = {
            "required": True,
            "current": current_retain,
            "satisfied": current_retain,
            "sources": list(retain_required_by),
        }
        overall = overall and current_retain

    return {"satisfied": overall, "topics": topics, "retain": retain}
