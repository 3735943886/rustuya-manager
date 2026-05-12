"""
Parity tests for pyrustuyabridge helper functions.

These verify that the Python-exposed bindings behave identically to the bridge's
internal Rust functions. The intent is *not* to test edge-case branches in
manager code (there are none — the manager defers to these helpers), but to
guarantee that the binding layer doesn't drift from the bridge.

Each fixture is named after the bridge concept it covers; failures here mean
the binding glue in `pyrustuyabridge/src/lib.rs` is wrong, not the manager.

Note on the `{root}` placeholder:
- The bridge does `{root}` substitution as a separate step (via
  `Cli::mqtt_topics`), then hands the post-substituted template to
  `match_topic` / `compile_topic_regex` / `tpl_to_wildcard`.
- Manager-side callers must do the same: substitute `{root}` first, then pass
  the result to these helpers.
"""

import pyrustuyabridge as pb

ROOT = "rustuya"


def _resolve_root(template: str, root: str = ROOT) -> str:
    """Mirrors the bridge's `{root}` pre-substitution step."""
    return pb.render_template(template, {"root": root})


# ─────────────────────────────────────────────────────────────────────────────
# tpl_to_wildcard
# ─────────────────────────────────────────────────────────────────────────────


class TestTplToWildcard:
    def test_default_event_template(self):
        assert pb.tpl_to_wildcard("{root}/event/{type}/{id}", ROOT) == "rustuya/event/+/+"

    def test_default_command_template(self):
        assert pb.tpl_to_wildcard("{root}/command", ROOT) == "rustuya/command"

    def test_default_message_template(self):
        assert pb.tpl_to_wildcard("{root}/{level}/{id}", ROOT) == "rustuya/+/+"

    def test_per_dp_event_template(self):
        # Regression: 71e001d — `mqtt-event-topic` with `{name}/{dp}/state` form
        assert pb.tpl_to_wildcard("tuya/{name}/{dp}/state", ROOT) == "tuya/+/+/state"

    def test_custom_root_prefix(self):
        assert (
            pb.tpl_to_wildcard("{root}/event/{type}/{id}", "myhome/tuya") == "myhome/tuya/event/+/+"
        )

    def test_command_with_id(self):
        # Bridge accepts command_topic with embedded {id} for per-device commands
        assert pb.tpl_to_wildcard("{root}/command/{id}", ROOT) == "rustuya/command/+"

    def test_unknown_placeholder_kept(self):
        # Only TOPIC_WILDCARD_KEYS (id, name, dp, action, cid, type, level) become
        # wildcards. Unknown keys (e.g. {value}, {dps}, {timestamp}) are left intact.
        assert pb.tpl_to_wildcard("{root}/{value}/x", ROOT) == "rustuya/{value}/x"


# ─────────────────────────────────────────────────────────────────────────────
# match_topic
# ─────────────────────────────────────────────────────────────────────────────


class TestMatchTopic:
    def test_event_topic_extracts_type_and_id(self):
        tpl = _resolve_root("{root}/event/{type}/{id}")
        assert pb.match_topic("rustuya/event/active/dev1", tpl) == {
            "type": "active",
            "id": "dev1",
        }

    def test_event_topic_no_match_on_different_prefix(self):
        tpl = _resolve_root("{root}/event/{type}/{id}")
        assert pb.match_topic("other/event/active/dev1", tpl) is None

    def test_message_topic_extracts_level_and_id(self):
        # Bridge default message topic: {root}/{level}/{id}
        tpl = _resolve_root("{root}/{level}/{id}")
        result = pb.match_topic("rustuya/error/dev1", tpl)
        assert result == {"level": "error", "id": "dev1"}

    def test_command_topic_with_id_and_action(self):
        # Custom: tuya/command/{id}/{action}
        tpl = _resolve_root("{root}/command/{id}/{action}")
        assert pb.match_topic("rustuya/command/dev1/set", tpl) == {
            "id": "dev1",
            "action": "set",
        }

    def test_per_dp_event_extracts_name_and_dp(self):
        # Regression: 71e001d
        tpl = _resolve_root("tuya/{name}/{dp}/state")
        assert pb.match_topic("tuya/kitchen/1/state", tpl) == {
            "name": "kitchen",
            "dp": "1",
        }

    def test_constant_template_exact_match(self):
        # Template with no {var}: match_topic falls back to exact string compare
        assert pb.match_topic("rustuya/command", "rustuya/command") == {}
        assert pb.match_topic("rustuya/command/x", "rustuya/command") is None

    def test_variable_cannot_span_slash(self):
        # Each {var} captures `[^/]+` — no slash allowed inside the captured group
        tpl = _resolve_root("{root}/event/{type}/{id}")
        assert pb.match_topic("rustuya/event/active/dev/extra", tpl) is None

    def test_variable_in_id_with_special_chars(self):
        # Regression: 6e6c478 — topic matching edge cases. Bridge's matcher
        # uses `[^/]+` per segment, so device IDs with dashes/dots are fine.
        tpl = _resolve_root("{root}/event/{type}/{id}")
        assert pb.match_topic("rustuya/event/active/bf-abc.123_xyz", tpl) == {
            "type": "active",
            "id": "bf-abc.123_xyz",
        }


# ─────────────────────────────────────────────────────────────────────────────
# render_template
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderTemplate:
    def test_substitutes_known_keys(self):
        assert (
            pb.render_template(
                "{root}/event/{type}/{id}",
                {"root": "rustuya", "type": "active", "id": "dev1"},
            )
            == "rustuya/event/active/dev1"
        )

    def test_leaves_unknown_keys_intact(self):
        # Behavior mirrors bridge: unknown keys produce `{key}` literally in output
        assert (
            pb.render_template("{root}/x/{unknown}/y", {"root": "rustuya"})
            == "rustuya/x/{unknown}/y"
        )

    def test_empty_vars(self):
        assert pb.render_template("{root}/x", {}) == "{root}/x"

    def test_no_placeholders(self):
        assert pb.render_template("rustuya/static/path", {"id": "ignored"}) == (
            "rustuya/static/path"
        )

    def test_partial_substitution(self):
        # Useful pattern: substitute {root} first, leaving wildcards intact
        assert (
            pb.render_template("{root}/event/{type}/{id}", {"root": "myhome/tuya"})
            == "myhome/tuya/event/{type}/{id}"
        )

    def test_command_topic_for_publish(self):
        # Manager-side use case: building concrete command topic before publish
        cmd_tpl = "{root}/command"
        assert pb.render_template(cmd_tpl, {"root": "rustuya"}) == "rustuya/command"


# ─────────────────────────────────────────────────────────────────────────────
# parse_payload
# ─────────────────────────────────────────────────────────────────────────────


class TestParsePayload:
    def test_json_object_merges_topic_vars(self):
        # Payload provides core data, topic vars are merged (without overriding)
        result = pb.parse_payload(
            '{"action": "set", "dps": {"1": true}}',
            {"id": "dev1"},
        )
        assert result["action"] == "set"
        assert result["dps"] == {"1": True}
        assert result["id"] == "dev1"  # merged from topic vars

    def test_topic_vars_do_not_override_payload(self):
        # Payload is authoritative; topic vars fill missing keys only
        result = pb.parse_payload('{"id": "from-payload"}', {"id": "from-topic"})
        assert result["id"] == "from-payload"

    def test_scalar_payload_with_dp_var_becomes_dps_dict(self):
        # Regression: single-DP mode. Topic carries {dp}, payload is the value.
        # Bridge wraps it as {"dps": {dp: value}}.
        result = pb.parse_payload("42", {"id": "dev1", "dp": "1"})
        assert result["dps"] == {"1": 42}
        assert result["id"] == "dev1"
        assert result["dp"] == "1"

    def test_scalar_payload_without_dp_becomes_payload_field(self):
        # No {dp} in topic → payload kept as generic "payload" field
        result = pb.parse_payload("hello", {"id": "dev1"})
        assert result["payload"] == "hello"
        assert result["id"] == "dev1"

    def test_json_value_wrapped_payload(self):
        # Regression: 9c19e96 — payload template `{"val": {value}}` style.
        # The payload is already a parsed JSON object with a `val` field.
        result = pb.parse_payload('{"val": 100}', {"id": "dev1"})
        assert result["val"] == 100
        assert result["id"] == "dev1"

    def test_set_action_heuristic(self):
        # Bridge's heuristic: if action=set and no `dps`/`data` key, treat
        # the remaining fields as the dps payload.
        result = pb.parse_payload(
            '{"action": "set", "1": true, "2": 50}',
            {"id": "dev1"},
        )
        assert result["action"] == "set"
        # dps should contain the remaining fields (1 and 2)
        assert "dps" in result
        assert result["dps"] == {"1": True, "2": 50}

    def test_array_payload_merges_per_item(self):
        # Bridge supports list payloads — each item gets topic vars merged
        result = pb.parse_payload(
            '[{"action": "get"}, {"action": "set", "dps": {"1": true}}]',
            {"id": "dev1"},
        )
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["id"] == "dev1"
        assert result[1]["id"] == "dev1"

    def test_invalid_json_treated_as_string(self):
        # Regression: 6e6c478 — non-JSON payload (bare string) handled gracefully
        result = pb.parse_payload("not-json", {"id": "dev1"})
        assert result["payload"] == "not-json"
        assert result["id"] == "dev1"

    def test_empty_payload(self):
        # Regression: 9d42560 — retain-clearing publishes empty string. We expect
        # the helper not to crash. (The manager should still filter these out
        # before processing, but the helper itself must be safe.)
        result = pb.parse_payload("", {"id": "dev1"})
        # Empty string is not valid JSON → falls into the "non-JSON → string" branch
        assert result["payload"] == ""
        assert result["id"] == "dev1"


# ─────────────────────────────────────────────────────────────────────────────
# Integration: full manager-style pipeline on a custom user template
# ─────────────────────────────────────────────────────────────────────────────


class TestIntegration:
    """Simulates the manager's full subscribe-and-decode flow on a custom topic
    setup, to verify the helpers compose correctly."""

    def test_custom_event_template_end_to_end(self):
        # User's bridge config:
        root = "myhome/tuya"
        raw_event_tpl = "{root}/event/{type}/{name}/{dp}/state"
        # Step 1: bridge-style {root} substitution (manager does this once)
        event_tpl = pb.render_template(raw_event_tpl, {"root": root})
        assert event_tpl == "myhome/tuya/event/{type}/{name}/{dp}/state"

        # Step 2: compute MQTT subscription wildcard
        sub = pb.tpl_to_wildcard(event_tpl, root)
        assert sub == "myhome/tuya/event/+/+/+/state"

        # Step 3: an incoming event arrives — extract topic vars and merge with payload
        incoming_topic = "myhome/tuya/event/active/kitchen_light/1/state"
        incoming_payload = "true"

        vars_ = pb.match_topic(incoming_topic, event_tpl)
        assert vars_ == {"type": "active", "name": "kitchen_light", "dp": "1"}

        parsed = pb.parse_payload(incoming_payload, vars_)
        # Because {dp} is present in topic vars, the bare scalar payload becomes
        # the value of dps["1"]
        assert parsed["dps"] == {"1": True}
        assert parsed["name"] == "kitchen_light"
        assert parsed["dp"] == "1"
        assert parsed["type"] == "active"
