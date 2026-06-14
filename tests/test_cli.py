"""Unit tests for cli helpers that don't need a broker.

The full embedded-bridge flow is covered in `tests/test_e2e_bridge.py`
(real broker, real PyBridgeServer); the tests here exercise the bits
that should hold even when neither is available — argument shaping,
default derivation, kwarg propagation to PyBridgeServer, and the
in-process supervisor that owns the embedded bridge's lifetime.
"""

from __future__ import annotations

import threading
import time
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


def _stub_pyrustuyabridge(monkeypatch):
    """Replace `pb.PyBridgeServer` with a clean-exit stub so the
    supervisor's thread runs without actually constructing a Rust
    server. Returns the list it appends every kwargs dict to, so
    callers can inspect what the supervisor passed in."""
    import pyrustuyabridge as pb

    seen: list[dict] = []

    def stub(**kw):
        seen.append(kw)
        srv = MagicMock()
        srv.start.return_value = None  # clean exit triggers respawn
        srv.stop.return_value = None
        return srv

    monkeypatch.setattr(pb, "PyBridgeServer", stub)
    return seen


def _drain_supervisor(supervisor, thread):
    """Stop the supervisor and wait for its daemon thread to exit so
    tests don't leak a busy loop into the next test."""
    supervisor.stop()
    thread.join(timeout=2.0)


class TestSpawnEmbeddedBridgeKwargs:
    """`_spawn_embedded_bridge` must build the PyBridgeServer kwargs from
    the manager's CLI args. The interesting bits:
      - manager-owned values (broker, root, state_file, log_level) are
        always present
      - `config_path` only appears when --bridge-config was provided
        (so the bridge falls back to defaults otherwise)

    The supervisor stores kwargs on construction; inspect `_kwargs` for
    a race-free read instead of waiting for the daemon thread to invoke
    PyBridgeServer.
    """

    def test_default_state_file_is_next_to_cloud_path(self, tmp_path, monkeypatch):
        from rustuya_manager.cli import _spawn_embedded_bridge

        _stub_pyrustuyabridge(monkeypatch)
        sup, t = _spawn_embedded_bridge(_make_args(tmp_path))
        try:
            # `rustuya.json` next to the (still-nonexistent) cloud file —
            # matches the standalone bridge's DEFAULT_STATE_FILE.
            assert sup._kwargs["state_file"] == str(tmp_path / "rustuya.json")
            # `config_path` must NOT be in kwargs when --bridge-config wasn't set —
            # otherwise pyrustuyabridge would try to auto-create a file at None.
            assert "config_path" not in sup._kwargs
        finally:
            _drain_supervisor(sup, t)

    def test_explicit_bridge_state_wins(self, tmp_path, monkeypatch):
        from rustuya_manager.cli import _spawn_embedded_bridge

        _stub_pyrustuyabridge(monkeypatch)
        explicit = str(tmp_path / "elsewhere" / "state.json")
        sup, t = _spawn_embedded_bridge(_make_args(tmp_path, bridge_state=explicit))
        try:
            assert sup._kwargs["state_file"] == explicit
        finally:
            _drain_supervisor(sup, t)

    def test_bridge_config_propagates_as_config_path(self, tmp_path, monkeypatch):
        from rustuya_manager.cli import _spawn_embedded_bridge

        _stub_pyrustuyabridge(monkeypatch)
        cfg = str(tmp_path / "bridge-config.json")
        sup, t = _spawn_embedded_bridge(_make_args(tmp_path, bridge_config=cfg))
        try:
            # Must be stored under the kwarg name pyrustuyabridge expects.
            assert sup._kwargs["config_path"] == cfg
            # Manager-owned settings still present alongside.
            assert sup._kwargs["mqtt_broker"] == "mqtt://localhost:1883"
            assert sup._kwargs["mqtt_root_topic"] == "test_root"
            assert sup._kwargs["log_level"] == "warn"
        finally:
            _drain_supervisor(sup, t)

    def test_mqtt_credentials_propagate_when_set(self, tmp_path, monkeypatch):
        # --mqtt-user/--mqtt-pass (or their env fallback) forward to the
        # embedded bridge as kwargs so a single-process deploy is configured
        # once. pyrustuyabridge resolves kwargs > config file, so these win.
        from rustuya_manager.cli import _spawn_embedded_bridge

        _stub_pyrustuyabridge(monkeypatch)
        sup, t = _spawn_embedded_bridge(_make_args(tmp_path, mqtt_user="u", mqtt_pass="p"))
        try:
            assert sup._kwargs["mqtt_user"] == "u"
            assert sup._kwargs["mqtt_password"] == "p"
        finally:
            _drain_supervisor(sup, t)

    def test_no_mqtt_credentials_means_no_cred_kwargs(self, tmp_path, monkeypatch):
        # Unauthenticated broker: the credential kwargs must be absent entirely
        # (not None), so the binding falls through to config-file/defaults.
        from rustuya_manager.cli import _spawn_embedded_bridge

        _stub_pyrustuyabridge(monkeypatch)
        sup, t = _spawn_embedded_bridge(_make_args(tmp_path))
        try:
            assert "mqtt_user" not in sup._kwargs
            assert "mqtt_password" not in sup._kwargs
        finally:
            _drain_supervisor(sup, t)

    def test_no_signals_always_set(self, tmp_path, monkeypatch):
        # The manager owns SIGINT/SIGTERM (run() installs loop handlers),
        # so the embedded bridge must be told NOT to install its own —
        # otherwise two handlers race in one process. The supervisor
        # forces no_signals=True regardless of caller input (§1.3).
        from rustuya_manager.cli import _spawn_embedded_bridge

        _stub_pyrustuyabridge(monkeypatch)
        sup, t = _spawn_embedded_bridge(_make_args(tmp_path))
        try:
            assert sup._kwargs["no_signals"] is True
        finally:
            _drain_supervisor(sup, t)


class TestResolveMqttCredentials:
    """Broker credentials fall back to env vars; an explicit flag always wins.
    Values are never logged (only presence), so these tests cover resolution."""

    def test_env_fallback_fills_missing(self, monkeypatch):
        from rustuya_manager.cli import _resolve_mqtt_credentials

        monkeypatch.setenv("RUSTUYA_MQTT_USER", "envu")
        monkeypatch.setenv("RUSTUYA_MQTT_PASSWORD", "envp")
        args = SimpleNamespace(mqtt_user=None, mqtt_pass=None)
        _resolve_mqtt_credentials(args)
        assert args.mqtt_user == "envu"
        assert args.mqtt_pass == "envp"

    def test_flag_wins_over_env(self, monkeypatch):
        from rustuya_manager.cli import _resolve_mqtt_credentials

        monkeypatch.setenv("RUSTUYA_MQTT_USER", "envu")
        monkeypatch.setenv("RUSTUYA_MQTT_PASSWORD", "envp")
        args = SimpleNamespace(mqtt_user="flagu", mqtt_pass="flagp")
        _resolve_mqtt_credentials(args)
        assert args.mqtt_user == "flagu"
        assert args.mqtt_pass == "flagp"

    def test_absent_everywhere_stays_none(self, monkeypatch):
        from rustuya_manager.cli import _resolve_mqtt_credentials

        monkeypatch.delenv("RUSTUYA_MQTT_USER", raising=False)
        monkeypatch.delenv("RUSTUYA_MQTT_PASSWORD", raising=False)
        args = SimpleNamespace(mqtt_user=None, mqtt_pass=None)
        _resolve_mqtt_credentials(args)
        assert args.mqtt_user is None
        assert args.mqtt_pass is None


class TestEmbeddedBridgeSupervisor:
    """Behaviour of the in-process supervisor wrapping PyBridgeServer.

    The supervisor exists because rustuya-bridge's `reconfigure` action
    self-terminates `run()` via the same cancellation token that
    `stop()` trips — from the outside, a reconfigure exit and a stop
    exit are indistinguishable on the bridge side. The supervisor
    distinguishes them on the MANAGER side via its own stop-Event,
    so reconfigure ⇒ respawn, stop ⇒ exit.
    """

    def test_no_signals_is_forced_even_if_caller_overrides(self):
        from rustuya_manager.cli import _EmbeddedBridgeSupervisor

        sup = _EmbeddedBridgeSupervisor(no_signals=False)
        assert sup._kwargs["no_signals"] is True

    def test_stop_before_run_does_not_spawn_any_server(self, monkeypatch):
        from rustuya_manager.cli import _EmbeddedBridgeSupervisor

        seen = _stub_pyrustuyabridge(monkeypatch)
        sup = _EmbeddedBridgeSupervisor()
        sup.stop()
        sup.run()  # synchronous; should return immediately
        assert seen == [], "no PyBridgeServer should be constructed once stop is set"

    def test_respawns_on_clean_exit_until_rate_limit(self, monkeypatch):
        """Each clean exit (reconfigure path) triggers a respawn.
        Without an external stop the supervisor still terminates when
        the rate limit fires — proving the cap is wired."""
        from rustuya_manager.cli import _EmbeddedBridgeSupervisor

        seen = _stub_pyrustuyabridge(monkeypatch)
        # Tighten the cap so the test exits quickly via the rate limit.
        monkeypatch.setattr(_EmbeddedBridgeSupervisor, "_MAX_RESTARTS_IN_WINDOW", 3)
        monkeypatch.setattr(_EmbeddedBridgeSupervisor, "_WINDOW_SEC", 5.0)

        sup = _EmbeddedBridgeSupervisor()
        t = threading.Thread(target=sup.run, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "supervisor should have given up after rate limit"
        # Initial spawn (1) plus _MAX_RESTARTS_IN_WINDOW respawns (3) = 4 servers.
        assert len(seen) == 4
        assert sup.restart_count == 3

    def test_stop_during_crash_backoff_is_interruptible(self, monkeypatch):
        """A long crash backoff must yield to stop() — otherwise
        shutdown could block for the full _CRASH_BACKOFF_SEC every
        time the bridge happened to crash near shutdown."""
        import pyrustuyabridge as pb

        from rustuya_manager.cli import _EmbeddedBridgeSupervisor

        # Use a very long backoff so the test reliably catches the
        # supervisor inside it.
        backoff = 30.0
        monkeypatch.setattr(_EmbeddedBridgeSupervisor, "_CRASH_BACKOFF_SEC", backoff)

        crashed = threading.Event()

        def stub(**kw):
            srv = MagicMock()
            srv.start.side_effect = RuntimeError("simulated crash")
            srv.stop.return_value = None
            crashed.set()
            return srv

        monkeypatch.setattr(pb, "PyBridgeServer", stub)

        sup = _EmbeddedBridgeSupervisor()
        t = threading.Thread(target=sup.run, daemon=True)
        t_start = time.monotonic()
        t.start()
        assert crashed.wait(timeout=2.0), "first crash didn't happen in time"
        # Give the supervisor a moment to enter Event.wait()
        time.sleep(0.05)
        sup.stop()
        t.join(timeout=2.0)
        elapsed = time.monotonic() - t_start
        assert not t.is_alive()
        # Must exit well before the full backoff window — that's the
        # whole point of the Event.wait + stop() pattern.
        assert elapsed < backoff / 4, (
            f"stop() did not interrupt the crash backoff (took {elapsed:.2f}s)"
        )


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

    def test_fills_broker_when_cli_not_passed(self, tmp_path):
        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, mqtt_broker="mqtt://from-cfg:1883")
        # None is the parser default — distinguishes "flag absent" from
        # "user passed a value that happens to equal manager's default".
        args = _make_args(tmp_path, embed_bridge=True, bridge_config=cfg, broker=None)
        _apply_bridge_config_defaults(args)
        assert args.broker == "mqtt://from-cfg:1883"

    def test_fills_root_when_cli_not_passed(self, tmp_path):
        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, mqtt_root_topic="root-from-cfg")
        args = _make_args(tmp_path, embed_bridge=True, bridge_config=cfg, root=None)
        _apply_bridge_config_defaults(args)
        assert args.root == "root-from-cfg"

    def test_explicit_default_value_is_not_overridden_by_cfg(self, tmp_path, caplog):
        """A-2 pin: if the user explicitly passes --broker with the same
        string that happens to equal manager's DEFAULT_BROKER, that value
        must win over bridge-config — not be silently replaced. Before A-2
        the precedence check was `args.broker == DEFAULT_BROKER`, which
        could not tell "argparse filled the default" from "user passed
        the default value explicitly"; both fell into the "fill from
        bridge-config" branch. The sentinel-None default fixes that."""
        import logging

        from rustuya_manager.cli import DEFAULT_BROKER, _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, mqtt_broker="mqtt://from-cfg:1883")
        # broker is explicitly DEFAULT_BROKER — as if the user typed it on
        # the CLI or wrote it into a docker env var.
        args = _make_args(tmp_path, embed_bridge=True, bridge_config=cfg, broker=DEFAULT_BROKER)
        with caplog.at_level(logging.WARNING):
            _apply_bridge_config_defaults(args)
        assert args.broker == DEFAULT_BROKER
        # The disagreement should be surfaced as a warning so the
        # contradiction isn't silent.
        assert "broker" in caplog.text.lower() and "disagree" in caplog.text.lower()

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

    def test_fills_state_file_when_cli_unset(self, tmp_path):
        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, state_file="/var/lib/rustuya/state.json")
        # bridge_state=None means the user didn't pass --bridge-state.
        args = _make_args(tmp_path, embed_bridge=True, bridge_config=cfg, bridge_state=None)
        _apply_bridge_config_defaults(args)
        assert args.bridge_state == "/var/lib/rustuya/state.json"

    def test_cli_state_file_wins_over_bridge_config(self, tmp_path, caplog):
        import logging

        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, state_file="/from/cfg/state.json")
        args = _make_args(
            tmp_path,
            embed_bridge=True,
            bridge_config=cfg,
            bridge_state="/from/cli/state.json",
        )
        with caplog.at_level(logging.WARNING):
            _apply_bridge_config_defaults(args)
        assert args.bridge_state == "/from/cli/state.json"
        assert "bridge-state" in caplog.text.lower() and "disagree" in caplog.text.lower()

    def test_matching_state_file_does_not_warn(self, tmp_path, caplog):
        import logging

        from rustuya_manager.cli import _apply_bridge_config_defaults

        cfg = self._write_cfg(tmp_path, state_file="/same/state.json")
        args = _make_args(
            tmp_path,
            embed_bridge=True,
            bridge_config=cfg,
            bridge_state="/same/state.json",
        )
        with caplog.at_level(logging.WARNING):
            _apply_bridge_config_defaults(args)
        assert "disagree" not in caplog.text.lower()


class TestApplyManagerDefaults:
    """`_apply_manager_defaults` runs AFTER `_apply_bridge_config_defaults`
    and fills any still-None sentinel values with the manager's own
    defaults — i.e. the bottom of the precedence chain. Tests verify the
    fill happens for unset flags and that non-None values are preserved."""

    def test_fills_broker_when_still_none(self, tmp_path):
        from rustuya_manager.cli import DEFAULT_BROKER, _apply_manager_defaults

        args = _make_args(tmp_path, broker=None)
        _apply_manager_defaults(args)
        assert args.broker == DEFAULT_BROKER

    def test_fills_root_when_still_none(self, tmp_path):
        from rustuya_manager.cli import DEFAULT_ROOT, _apply_manager_defaults

        args = _make_args(tmp_path, root=None)
        _apply_manager_defaults(args)
        assert args.root == DEFAULT_ROOT

    def test_preserves_user_set_values(self, tmp_path):
        from rustuya_manager.cli import _apply_manager_defaults

        args = _make_args(tmp_path, broker="mqtt://x:1883", root="myroot")
        _apply_manager_defaults(args)
        assert args.broker == "mqtt://x:1883"
        assert args.root == "myroot"
