"""Unit tests for cli helpers that don't need a broker.

The full embedded-bridge flow is covered in `tests/test_e2e_bridge.py`
(real broker, real PyBridgeServer); the tests here exercise the bits
that should hold even when neither is available — argument shaping,
default derivation, kwarg propagation to PyBridgeServer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_args(tmp_path, **overrides) -> SimpleNamespace:
    base = {
        "broker": "mqtt://localhost:1883",
        "root": "test_root",
        "cloud": str(tmp_path / "tuyadevices.json"),
        "bridge_state": None,
        "log_level": "warn",
        "bridge_config": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestSpawnEmbeddedBridgeKwargs:
    """`_spawn_embedded_bridge` must build the PyBridgeServer kwargs from
    the manager's CLI args. The interesting bits:
      - manager-owned values (broker, root, state_file, log_level) are
        always present
      - `config_path` only appears when --bridge-config was provided
        (so the bridge falls back to defaults otherwise)
    """

    def test_default_state_file_is_next_to_cloud_path(self, tmp_path, monkeypatch):
        import pyrustuyabridge as pb

        from rustuya_manager.cli import _spawn_embedded_bridge

        seen: dict = {}
        monkeypatch.setattr(pb, "PyBridgeServer", lambda **kw: seen.update(kw) or MagicMock())

        args = _make_args(tmp_path)
        _spawn_embedded_bridge(args)
        # `bridge-state.json` next to the (still-nonexistent) cloud file.
        assert seen["state_file"] == str(tmp_path / "bridge-state.json")
        # `config_path` must NOT be in kwargs when --bridge-config wasn't set —
        # otherwise pyrustuyabridge would try to auto-create a file at None.
        assert "config_path" not in seen

    def test_explicit_bridge_state_wins(self, tmp_path, monkeypatch):
        import pyrustuyabridge as pb

        from rustuya_manager.cli import _spawn_embedded_bridge

        seen: dict = {}
        monkeypatch.setattr(pb, "PyBridgeServer", lambda **kw: seen.update(kw) or MagicMock())

        explicit = str(tmp_path / "elsewhere" / "state.json")
        args = _make_args(tmp_path, bridge_state=explicit)
        _spawn_embedded_bridge(args)
        assert seen["state_file"] == explicit

    def test_bridge_config_propagates_as_config_path(self, tmp_path, monkeypatch):
        import pyrustuyabridge as pb

        from rustuya_manager.cli import _spawn_embedded_bridge

        seen: dict = {}
        monkeypatch.setattr(pb, "PyBridgeServer", lambda **kw: seen.update(kw) or MagicMock())

        cfg = str(tmp_path / "bridge-config.json")
        args = _make_args(tmp_path, bridge_config=cfg)
        _spawn_embedded_bridge(args)
        # Must be passed under the kwarg name pyrustuyabridge expects.
        assert seen["config_path"] == cfg
        # Manager-owned settings still present alongside.
        assert seen["mqtt_broker"] == "mqtt://localhost:1883"
        assert seen["mqtt_root_topic"] == "test_root"
        assert seen["log_level"] == "warn"


class TestPeekBridgeConfig:
    """`_peek_bridge_config` is a best-effort JSON read. Any failure mode
    should return {} so spawn-time can do the real loading + error
    reporting via pyrustuyabridge."""

    def test_returns_empty_dict_when_path_is_none(self):
        from rustuya_manager.cli import _peek_bridge_config

        assert _peek_bridge_config(None) == {}

    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        from rustuya_manager.cli import _peek_bridge_config

        assert _peek_bridge_config(str(tmp_path / "nope.json")) == {}

    def test_returns_empty_dict_on_invalid_json(self, tmp_path):
        from rustuya_manager.cli import _peek_bridge_config

        p = tmp_path / "broken.json"
        p.write_text("{ not valid json")
        assert _peek_bridge_config(str(p)) == {}

    def test_returns_empty_dict_when_top_level_is_not_object(self, tmp_path):
        from rustuya_manager.cli import _peek_bridge_config

        p = tmp_path / "list.json"
        p.write_text('["not", "an", "object"]')
        assert _peek_bridge_config(str(p)) == {}

    def test_returns_parsed_dict_on_well_formed_file(self, tmp_path):
        from rustuya_manager.cli import _peek_bridge_config

        p = tmp_path / "ok.json"
        p.write_text('{"mqtt_broker": "mqtt://x:1883", "mqtt_root_topic": "r"}')
        out = _peek_bridge_config(str(p))
        assert out == {"mqtt_broker": "mqtt://x:1883", "mqtt_root_topic": "r"}


class TestApplyBridgeConfigDefaults:
    """Precedence rule: CLI flag (non-default) > bridge-config value >
    manager default. Mismatch between CLI flag and bridge-config logs
    a warning but the CLI flag still wins (manager will use it for
    its own MQTT connection; the embedded bridge will end up with the
    same value because manager passes it as a kwarg)."""

    def _write_cfg(self, tmp_path, **fields):
        import json as _json

        p = tmp_path / "bridge.json"
        p.write_text(_json.dumps(fields))
        return str(p)

    def test_noop_without_embed_bridge_flag(self, tmp_path):
        from rustuya_manager.cli import DEFAULT_BROKER, _apply_bridge_config_defaults

        # bridge-config present but --embed-bridge not set → don't touch args.
        cfg = self._write_cfg(tmp_path, mqtt_broker="mqtt://from-cfg:1883", mqtt_root_topic="rcfg")
        args = _make_args(tmp_path, embed_bridge=False, bridge_config=cfg)
        _apply_bridge_config_defaults(args)
        assert args.broker == DEFAULT_BROKER  # untouched
        assert args.root == "test_root"  # _make_args set it explicitly; untouched

    def test_noop_without_bridge_config_flag(self, tmp_path):
        from rustuya_manager.cli import DEFAULT_BROKER, _apply_bridge_config_defaults

        args = _make_args(tmp_path, embed_bridge=True, bridge_config=None)
        _apply_bridge_config_defaults(args)
        assert args.broker == DEFAULT_BROKER  # nothing to fall back to

    def test_fills_broker_when_cli_left_at_default(self, tmp_path):
        from rustuya_manager.cli import DEFAULT_BROKER, _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, mqtt_broker="mqtt://from-cfg:1883")
        args = _make_args(tmp_path, embed_bridge=True, bridge_config=cfg, broker=DEFAULT_BROKER)
        _apply_bridge_config_defaults(args)
        assert args.broker == "mqtt://from-cfg:1883"

    def test_fills_root_when_cli_left_at_default(self, tmp_path):
        from rustuya_manager.cli import DEFAULT_ROOT, _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, mqtt_root_topic="root-from-cfg")
        args = _make_args(tmp_path, embed_bridge=True, bridge_config=cfg, root=DEFAULT_ROOT)
        _apply_bridge_config_defaults(args)
        assert args.root == "root-from-cfg"

    def test_cli_value_wins_over_bridge_config(self, tmp_path, caplog):
        import logging

        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(
            tmp_path, mqtt_broker="mqtt://from-cfg:1883", mqtt_root_topic="root-from-cfg"
        )
        args = _make_args(
            tmp_path,
            embed_bridge=True,
            bridge_config=cfg,
            broker="mqtt://cli-explicit:1883",
            root="cli-explicit-root",
        )
        with caplog.at_level(logging.WARNING):
            _apply_bridge_config_defaults(args)
        # CLI values preserved.
        assert args.broker == "mqtt://cli-explicit:1883"
        assert args.root == "cli-explicit-root"
        # Mismatch warning logged for each so the operator notices the
        # contradiction at startup.
        log = caplog.text
        assert "broker" in log.lower() and "disagree" in log.lower()
        assert "root" in log.lower()

    def test_matching_cli_and_cfg_does_not_warn(self, tmp_path, caplog):
        import logging

        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, mqtt_broker="mqtt://same:1883", mqtt_root_topic="same-root")
        args = _make_args(
            tmp_path,
            embed_bridge=True,
            bridge_config=cfg,
            broker="mqtt://same:1883",
            root="same-root",
        )
        with caplog.at_level(logging.WARNING):
            _apply_bridge_config_defaults(args)
        assert "disagree" not in caplog.text.lower()
