"""Tests for the general payload template parser (sentinel + tree walk).

Together these cover the substitution shapes a user is likely to pick in
their bridge's `mqtt_payload_template`. Each test pins one shape; if the
parser regresses, the failing test names the exact case.
"""

from __future__ import annotations

import pytest

from rustuya_manager.payload import (
    parse_payload_with_template,
    validate_payload_template,
)


class TestParse:
    def test_bare_scalar_value(self):
        # template `{value}` with payload `true`
        captures = parse_payload_with_template("true", "{value}")
        assert captures == {"value": True}

    def test_bare_scalar_string(self):
        captures = parse_payload_with_template('"hello"', "{value}")
        assert captures == {"value": "hello"}

    def test_object_wrapped_value(self):
        # Default-ish: {"v":{value}}
        captures = parse_payload_with_template('{"v":42}', '{"v":{value}}')
        assert captures == {"value": 42}

    def test_users_actual_template(self):
        # The exact template the test-server bridge has
        tpl = '{"type": "{type}", "value": {value}}'
        captures = parse_payload_with_template(
            '{"type": "active", "value": 13986}', tpl
        )
        assert captures == {"type": "active", "value": 13986}

    def test_value_is_array(self):
        # Sub-device offline event: value is an array
        captures = parse_payload_with_template(
            '{"type":"passive","value":["abc","def"]}',
            '{"type":"{type}","value":{value}}',
        )
        assert captures == {"type": "passive", "value": ["abc", "def"]}

    def test_value_is_nested_object(self):
        captures = parse_payload_with_template(
            '{"v":{"a":{"b":1}}}', '{"v":{value}}'
        )
        assert captures == {"value": {"a": {"b": 1}}}

    def test_bare_dps(self):
        captures = parse_payload_with_template(
            '{"1":true,"2":42}', "{dps}"
        )
        assert captures == {"dps": {"1": True, "2": 42}}

    def test_object_wrapped_dps(self):
        captures = parse_payload_with_template(
            '{"id":"abc","name":"lamp","data":{"1":true,"14":"off"}}',
            '{"id":"{id}","name":"{name}","data":{dps}}',
        )
        assert captures == {
            "id": "abc",
            "name": "lamp",
            "dps": {"1": True, "14": "off"},
        }

    def test_full_multi_var(self):
        # Most variables stuffed into one template
        tpl = '{"i":"{id}","n":"{name}","c":"{cid}","t":{timestamp},"v":{value}}'
        payload = '{"i":"abc","n":"lamp","c":"xyz","t":1700000000,"v":true}'
        captures = parse_payload_with_template(payload, tpl)
        assert captures == {
            "id": "abc",
            "name": "lamp",
            "cid": "xyz",
            "timestamp": 1700000000,
            "value": True,
        }

    def test_structure_mismatch_returns_none(self):
        # Template has 2 keys, payload has different keys
        assert parse_payload_with_template(
            '{"foo":1}', '{"v":{value}}'
        ) is None

    def test_literal_mismatch_returns_none(self):
        # Template has a fixed string at a position; payload differs
        assert parse_payload_with_template(
            '{"type":"OTHER","v":1}', '{"type":"FIXED","v":{value}}'
        ) is None

    def test_no_placeholders_returns_none(self):
        # A template without recognized placeholders has nothing to capture
        assert parse_payload_with_template(
            '{"v":1}', '{"v":1}'
        ) is None

    def test_invalid_payload_returns_none(self):
        assert parse_payload_with_template("not-json", "{value}") is None

    def test_non_json_template_returns_none(self):
        # Template that isn't valid JSON even with sentinel substitution
        # (e.g. text-style `key={value};other={timestamp}`)
        assert parse_payload_with_template(
            "v=1;t=2", "v={value};t={timestamp}"
        ) is None


class TestValidate:
    def test_default_bare_scalar(self):
        ok, _ = validate_payload_template("{value}")
        assert ok is True

    def test_users_actual_template(self):
        ok, _ = validate_payload_template('{"type": "{type}", "value": {value}}')
        assert ok is True

    def test_object_dps(self):
        ok, _ = validate_payload_template('{"id":"{id}","data":{dps}}')
        assert ok is True

    def test_no_placeholders_is_not_ok(self):
        ok, msg = validate_payload_template('{"foo":"bar"}')
        assert ok is False
        assert "no recognized placeholders" in msg.lower()

    def test_non_json_template_is_not_ok(self):
        ok, msg = validate_payload_template("v={value};t={timestamp}")
        assert ok is False
        assert "valid json" in msg.lower()

    def test_empty_template(self):
        ok, msg = validate_payload_template("")
        assert ok is False
        ok2, _ = validate_payload_template(None)
        assert ok2 is False
