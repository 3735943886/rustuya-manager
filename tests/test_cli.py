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
