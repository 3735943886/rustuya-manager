"""Unit tests for the pure plugin-requirement evaluator (requirements.py)."""

from __future__ import annotations

import pytest

from rustuya_manager.requirements import (
    TopicRequirement,
    evaluate,
    placeholders,
    propose_template,
    validate_requirement,
)


# ── placeholders ────────────────────────────────────────────────────────────
def test_placeholders_extracts_names():
    assert placeholders("{root}/event/{type}/{id}") == {"root", "type", "id"}
    assert placeholders(None) == set()
    assert placeholders("{root}/command") == {"root"}


# ── validate_requirement (declaration-time guardrails) ──────────────────────
def test_validate_rejects_unknown_template():
    with pytest.raises(ValueError, match="unknown template"):
        validate_requirement("nope", ("id",), ())


def test_validate_rejects_unknown_placeholder():
    with pytest.raises(ValueError, match="unknown placeholder"):
        validate_requirement("event", ("frobnicate",), ())


def test_validate_rejects_present_and_absent_same_placeholder():
    with pytest.raises(ValueError, match="both must_have and must_not_have"):
        validate_requirement("event", ("dp",), ("dp",))


def test_validate_rejects_removing_bridge_minimum():
    # {id} is in the event topic's default scheme → a routing minimum.
    with pytest.raises(ValueError, match="needs them for routing"):
        validate_requirement("event", (), ("id",))


def test_validate_accepts_reasonable_requirement():
    validate_requirement("event", ("dp",), ("name",))  # no raise


# ── propose_template ────────────────────────────────────────────────────────
def test_propose_appends_missing_placeholder():
    assert (
        propose_template("{root}/event/{type}/{id}", add={"dp"}, remove=set())
        == "{root}/event/{type}/{id}/{dp}"
    )


def test_propose_removes_standalone_segment_only():
    # {name} stands alone → removed; a mixed segment would be left untouched.
    assert propose_template("{root}/x/{name}", add=set(), remove={"name"}) == "{root}/x"
    assert propose_template("{root}/dp{dp}val", add=set(), remove={"dp"}) == "{root}/dp{dp}val"


# ── evaluate: empty / no-op ─────────────────────────────────────────────────
def test_evaluate_no_requirements_returns_none():
    assert evaluate({"mqtt_event_topic": "{root}/event/{type}/{id}"}, [], []) is None


# ── evaluate: missing placeholder ───────────────────────────────────────────
def test_evaluate_flags_missing_placeholder_and_recommends():
    cfg = {"mqtt_event_topic": "{root}/event/{type}/{id}"}
    reqs = [TopicRequirement("PluginA", "event", must_have=("dp",))]
    rep = evaluate(cfg, reqs, [])
    assert rep["satisfied"] is False
    ev = rep["topics"]["event"]
    assert ev["missing"] == ["dp"]
    assert ev["recommended"] == "{root}/event/{type}/{id}/{dp}"
    assert ev["sources"][0]["satisfied"] is False


def test_evaluate_satisfied_when_present():
    cfg = {"mqtt_event_topic": "{root}/event/{type}/{id}/{dp}"}
    reqs = [TopicRequirement("PluginA", "event", must_have=("dp",))]
    rep = evaluate(cfg, reqs, [])
    assert rep["satisfied"] is True
    assert rep["topics"]["event"]["satisfied"] is True


def test_evaluate_uses_default_when_config_key_absent():
    # No mqtt_command_topic in config → bridge default "{root}/command".
    reqs = [TopicRequirement("PluginA", "command", must_have=("id",))]
    rep = evaluate({}, reqs, [])
    ev = rep["topics"]["command"]
    assert ev["current"] == "{root}/command"
    assert ev["missing"] == ["id"]


# ── evaluate: present-wins conflict resolution ──────────────────────────────
def test_present_wins_over_absent_and_marks_unhonored():
    # PluginA needs {name} present on the event topic; PluginB wants it absent.
    # Present wins; B is reported unhonored (not satisfied-by-removal).
    cfg = {"mqtt_event_topic": "{root}/event/{type}/{id}/{name}"}
    reqs = [
        TopicRequirement("PluginA", "event", must_have=("name",)),
        TopicRequirement("PluginB", "event", must_not_have=("name",)),
    ]
    rep = evaluate(cfg, reqs, [])
    ev = rep["topics"]["event"]
    # {name} stays → A satisfied, nothing forbidden removed, topic satisfied.
    assert ev["satisfied"] is True
    assert ev["forbidden"] == []
    src = {s["source"]: s for s in ev["sources"]}
    assert src["PluginA"]["satisfied"] is True
    assert src["PluginB"]["unhonored"] == ["name"]


def test_must_not_have_honored_when_uncontested():
    # No one needs {name}; it's not a routing minimum → removal recommended.
    cfg = {"mqtt_event_topic": "{root}/event/{type}/{id}/{name}"}
    reqs = [TopicRequirement("PluginB", "event", must_not_have=("name",))]
    rep = evaluate(cfg, reqs, [])
    ev = rep["topics"]["event"]
    assert ev["satisfied"] is False
    assert ev["forbidden"] == ["name"]
    assert ev["recommended"] == "{root}/event/{type}/{id}"


# ── evaluate: retain (requires-True only) ───────────────────────────────────
def test_retain_required_but_off_is_unsatisfied():
    rep = evaluate({"mqtt_retain": False}, [], ["PluginA"])
    assert rep["retain"]["required"] is True
    assert rep["retain"]["satisfied"] is False
    assert rep["satisfied"] is False


def test_retain_required_and_on_is_satisfied():
    rep = evaluate({"mqtt_retain": True}, [], ["PluginA"])
    assert rep["retain"]["satisfied"] is True
    assert rep["satisfied"] is True


def test_retain_missing_key_treated_as_off():
    rep = evaluate({}, [], ["PluginA"])
    assert rep["retain"]["current"] is False
    assert rep["retain"]["satisfied"] is False
