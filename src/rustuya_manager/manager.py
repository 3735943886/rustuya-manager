"""Programmatic core: connect to a bridge, load cloud state, and drive it —
without pulling in the web UI.

`Manager` is the reusable async-context-manager counterpart of what
`cli.py`'s `run()` does for the CLI: resolve broker/credential defaults,
build `State` + `BridgeClient`, optionally spawn an embedded bridge, and
wait for bootstrap. Both `cli.py` and `web.py` build on top of it; library
callers can use it directly without installing the `[web]` extra:

    async with Manager(broker="mqtt://host:1883", root="rustuya") as m:
        await m.wait_ready()
        await m.add_device("bf1234...")
        # or the underlying API directly:
        await m.client.publish_command("add", target_id="bf1234...")
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pyrustuyabridge as pb

from .cloud import CloudFormatError, parse_cloud_json, save_cloud_json
from .diff import DiffResult
from .models import Device
from .mqtt import BridgeClient
from .scan import LanScanCoordinator
from .state import State
from .wizard import WizardManager

logger = logging.getLogger(__name__)

# Defaults are constants rather than inline literals so the "did the caller
# override?" check below has a stable thing to compare against — and so the
# bridge-config fallback knows which manager-side defaults are placeholders
# worth replacing.
DEFAULT_BROKER = "mqtt://localhost:1883"
DEFAULT_ROOT = "rustuya"

EventCallback = Callable[[str, dict[str, str], Any, dict[str, Any] | None], Awaitable[None]]


def _peek_bridge_config(path: str | None) -> dict:
    """Parse a `--bridge-config` JSON file just enough to surface its
    `mqtt_broker` / `mqtt_root_topic` to the manager.

    Returns `{}` when the path is None, missing, unreadable, or invalid —
    pyrustuyabridge's own loader will surface the *real* error at spawn time
    (with its own line numbers and context). The peek is a best-effort
    convenience read, not a validator.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _apply_bridge_config_defaults(args: Any) -> None:
    """If `--bridge-config` carries `mqtt_broker` / `mqtt_root_topic` /
    `state_file`, fill those into args when the caller did NOT set the
    corresponding field — so the caller only has to specify them in one
    place when embedding the bridge.

    Precedence: explicit value > bridge-config field > manager default. The
    three fields involved here (`broker`, `root`, `bridge_state`) are all
    `None` until set, so "caller provided?" can be distinguished from
    "still unset" — without that sentinel, a caller who explicitly passed
    `broker="mqtt://localhost:1883"` (the same string as the manager
    default) would silently lose to the bridge-config value.

    If the caller explicitly set a value AND it disagrees with the
    bridge-config value, a warning is logged because the embedded bridge
    will end up with the kwarg value (manager's explicit value) while the
    bridge-config file says something else — confusing on disk-diff.
    """
    if not args.embed_bridge or not args.bridge_config:
        return
    cfg = _peek_bridge_config(args.bridge_config)
    cfg_broker = cfg.get("mqtt_broker")
    cfg_root = cfg.get("mqtt_root_topic")
    cfg_state = cfg.get("state_file")

    if cfg_broker:
        if args.broker is None:
            args.broker = cfg_broker
            logger.info("Using broker %r from --bridge-config", cfg_broker)
        elif args.broker != cfg_broker:
            logger.warning(
                "--broker (%r) disagrees with --bridge-config mqtt_broker (%r); "
                "manager will use the CLI value, embedded bridge will follow.",
                args.broker,
                cfg_broker,
            )

    if cfg_root:
        if args.root is None:
            args.root = cfg_root
            logger.info("Using root %r from --bridge-config", cfg_root)
        elif args.root != cfg_root:
            logger.warning(
                "--root (%r) disagrees with --bridge-config mqtt_root_topic (%r); "
                "manager will use the CLI value, embedded bridge will follow.",
                args.root,
                cfg_root,
            )

    if cfg_state:
        if args.bridge_state is None:
            args.bridge_state = cfg_state
            logger.info("Using state_file %r from --bridge-config", cfg_state)
        elif args.bridge_state != cfg_state:
            logger.warning(
                "--bridge-state (%r) disagrees with --bridge-config state_file (%r); "
                "manager will use the CLI value, embedded bridge will follow.",
                args.bridge_state,
                cfg_state,
            )

    # Broker credentials: let --bridge-config supply them for the manager's own
    # connection too (the embedded single-process deploy shares one broker), but
    # the CLI flag / env always wins. Values are never logged — only presence —
    # to keep credentials out of logs.
    cfg_user = cfg.get("mqtt_user")
    cfg_pass = cfg.get("mqtt_password")
    if cfg_user:
        if args.mqtt_user is None:
            args.mqtt_user = cfg_user
            logger.info("Using mqtt_user from --bridge-config")
        elif args.mqtt_user != cfg_user:
            logger.warning(
                "--mqtt-user / RUSTUYA_MQTT_USER disagrees with --bridge-config "
                "mqtt_user; using the CLI/env value (embedded bridge follows the kwarg)."
            )
    if cfg_pass:
        if args.mqtt_pass is None:
            args.mqtt_pass = cfg_pass
        elif args.mqtt_pass != cfg_pass:
            logger.warning(
                "--mqtt-pass / RUSTUYA_MQTT_PASSWORD disagrees with --bridge-config "
                "mqtt_password; using the CLI/env value."
            )


def _apply_manager_defaults(args: Any) -> None:
    """Fill any still-`None` sentinel values with the manager's own
    defaults. Runs AFTER `_apply_bridge_config_defaults` so the precedence
    chain `explicit > bridge-config > manager default` resolves bottom-up
    without losing the "caller provided?" signal."""
    if args.broker is None:
        args.broker = DEFAULT_BROKER
    if args.root is None:
        args.root = DEFAULT_ROOT


def _resolve_mqtt_credentials(args: Any) -> None:
    """Fill broker credentials from the environment when unset.

    Precedence: explicit `mqtt_user`/`mqtt_pass` > `RUSTUYA_MQTT_USER`/
    `RUSTUYA_MQTT_PASSWORD` env. Prefer the env vars in production — a
    password passed as a CLI flag is visible in the host's process list
    (`ps`). Runs before `_apply_bridge_config_defaults` so the resolved
    value is what the bridge-config precedence check compares against."""
    if args.mqtt_user is None:
        args.mqtt_user = os.environ.get("RUSTUYA_MQTT_USER") or None
    if args.mqtt_pass is None:
        args.mqtt_pass = os.environ.get("RUSTUYA_MQTT_PASSWORD") or None


def _load_cloud(path: Path) -> dict[str, Device]:
    with path.open() as f:
        data = json.load(f)
    iterable = data if isinstance(data, list) else data.values()
    return {d["id"]: Device.from_dict(d) for d in iterable if "id" in d}


class _EmbeddedBridgeSupervisor:
    """Owns the embedded `PyBridgeServer` across its full lifetime,
    including respawn for the bridge's reconfigure path.

    Why a supervisor is required, not optional. rustuya-bridge's
    `reconfigure` action (added in 0.3.0-rc.9 / Python 0.2.0-rc.9) ends
    `run()` via the same internal `CancellationToken` that `stop()`
    trips — so from the outside, a reconfigure exit and a stop exit
    look identical: `start()` returns normally with no exception. The
    bridge documents the contract as "always restarts, supervisor
    expected" (systemd's `Restart=always` on the standalone deploy).
    Embedded in the manager, the equivalent has to live in-process —
    that's what this class is.

    Loop shape:
      1. Construct a fresh `PyBridgeServer` (§1.4 of internals.md
         requires a new instance per iteration; the binding rejects
         reuse).
      2. Call `start()`. It blocks until `stop()` is called externally,
         the bridge self-terminates via reconfigure, or a Rust-side
         error is raised.
      3. If `stop()` was requested, exit the loop.
         If `start()` returned cleanly without our stop, respawn
         immediately (reconfigure path).
         If `start()` raised, log + back off for `_CRASH_BACKOFF_SEC`,
         then respawn — unless the rate limit (`_MAX_RESTARTS_IN_WINDOW`
         in `_WINDOW_SEC`) has been hit, in which case the supervisor
         gives up.

    Concurrency:
      The supervisor runs as an `asyncio.Task` on the manager's event
      loop, so `run()` and `stop()` execute on the same loop thread and
      never preempt each other mid-statement — no lock is needed to guard
      the live-server reference. `stop()` is called from the asyncio
      shutdown path (`_close_embedded_bridge`). PyBridgeServer's `stop()`
      itself is sync and lock-free (out-of-mutex cancellation token,
      §1.3), so tripping it from the loop never blocks.
    """

    # If the bridge exits more than this many times within the window,
    # the supervisor stops respawning and surfaces an error. Tight
    # enough to catch a config-broken tight-loop; loose enough that a
    # busy operator can issue `reconfigure` a few times in a row
    # without tripping it.
    _MAX_RESTARTS_IN_WINDOW = 5
    _WINDOW_SEC = 30.0
    _CRASH_BACKOFF_SEC = 5.0

    def __init__(self, **kwargs: Any) -> None:
        # no_signals=True is forced, matching the pre-supervisor
        # behaviour (internals.md §1.3 explains why the manager owns
        # SIGINT/SIGTERM and the embedded bridge must not install
        # competing handlers).
        self._kwargs = {**kwargs, "no_signals": True}
        self._stop = asyncio.Event()
        self._server: Any = None
        self._restart_count = 0  # public via the .restart_count attribute

    @property
    def restart_count(self) -> int:
        """Number of times the bridge has been respawned since the
        supervisor started. Includes both reconfigure-driven and
        crash-driven restarts; counted AFTER the spawn completes."""
        return self._restart_count

    async def run(self) -> None:
        """Supervisor-task entry. Returns when `stop()` has been called
        or the rate limit has been exceeded.

        `start_async()` runs the bridge on `pyo3-async-runtimes`' own
        multi-threaded tokio runtime and resolves only when the server
        shuts down; awaiting it blocks this task exactly as `start()`
        blocked the daemon thread before (internals.md §1.2). A
        Rust-side failure surfaces here as a raised exception; cancellation
        of this task (a `CancelledError`, a `BaseException`) is NOT caught,
        so it propagates and lets the bridge's own cleanup run."""
        exits: list[float] = []
        while not self._stop.is_set():
            crashed = False
            try:
                self._server = pb.PyBridgeServer(**self._kwargs)
                await self._server.start_async()
            except Exception:  # noqa: BLE001 - any failure flows through respawn
                crashed = True
                logger.exception("embedded bridge: error during construction or start")

            if self._stop.is_set():
                return

            now = time.monotonic()
            exits = [t for t in exits if now - t < self._WINDOW_SEC]
            exits.append(now)
            if len(exits) > self._MAX_RESTARTS_IN_WINDOW:
                logger.error(
                    "embedded bridge exited %d times in the last %.0fs — giving up. "
                    "Inspect the bridge config / logs and restart the manager to retry.",
                    len(exits),
                    self._WINDOW_SEC,
                )
                return

            self._restart_count += 1
            if crashed:
                logger.warning(
                    "embedded bridge will be respawned in %.1fs", self._CRASH_BACKOFF_SEC
                )
                # wait_for resolves as soon as stop() sets the event,
                # letting shutdown interrupt the backoff for a fast exit
                # instead of always waiting the full window; on timeout
                # the backoff elapsed and we respawn.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._CRASH_BACKOFF_SEC)
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                logger.info(
                    "embedded bridge exited cleanly (reconfigure or self-terminate); respawning"
                )

    def stop(self) -> None:
        """Signal the supervisor to exit. Trips the live server's
        cancellation token (if any) and prevents the loop from
        starting a fresh one. Idempotent; called on the event loop
        (no preemption against `run()`, so no lock needed)."""
        self._stop.set()
        srv = self._server
        if srv is None:
            return
        try:
            srv.stop()
        except Exception:  # noqa: BLE001 - best-effort; the loop will exit anyway
            logger.exception("stop() on the embedded bridge failed; supervisor will exit anyway")


def _spawn_embedded_bridge(
    args: Any,
) -> tuple[_EmbeddedBridgeSupervisor, asyncio.Task]:
    """Build the embedded-bridge supervisor and start its supervisor task.

    The supervisor (see `_EmbeddedBridgeSupervisor`) owns the per-iteration
    `PyBridgeServer` lifecycle so the bridge's `reconfigure` action — which
    self-terminates the bridge to apply a fresh config — gets a fresh
    process equivalent in-process. Returns `(supervisor, task)`; the
    caller drives shutdown via `supervisor.stop()` + `await`-ing the task.
    Must be called from within the running event loop (it schedules the
    supervisor with `asyncio.create_task`).
    """
    default_state = Path(args.cloud_path).resolve().parent / "rustuya.json"
    state_file = args.bridge_state or str(default_state)

    # The manager owns broker / root / state-file / log-level / broker creds —
    # the embedded bridge must agree with the manager's view on those, since
    # both connect to the same broker. Broker credentials are co-owned now (the
    # manager has its own authenticated connection): they come from
    # mqtt_user/mqtt_pass or the env, and are forwarded as kwargs here so a
    # single-process deploy configures them once. Everything else (custom
    # topics, scanner options, retain flag, …) stays the bridge's domain, read
    # from `bridge_config` when provided. pyrustuyabridge resolves
    # kwargs > config file > defaults, so these kwargs override the config file.
    # `no_signals=True` is set inside the supervisor (internals.md §1.3 —
    # manager owns SIGINT/SIGTERM, so the embedded bridge must NOT install its own).
    kwargs: dict[str, Any] = {
        "mqtt_broker": args.broker,
        "mqtt_root_topic": args.root,
        "state_file": state_file,
        "log_level": args.log_level,
    }
    if getattr(args, "bridge_config", None):
        kwargs["config_path"] = args.bridge_config
    # Forward broker credentials as kwargs (in-memory, so no process-list
    # exposure for the bridge). Only when set, so an unauthenticated broker
    # still gets a clean kwargs dict.
    if getattr(args, "mqtt_user", None):
        kwargs["mqtt_user"] = args.mqtt_user
    if getattr(args, "mqtt_pass", None):
        kwargs["mqtt_password"] = args.mqtt_pass

    supervisor = _EmbeddedBridgeSupervisor(**kwargs)
    task = asyncio.create_task(supervisor.run())
    return supervisor, task


async def _close_embedded_bridge(supervisor: _EmbeddedBridgeSupervisor) -> None:
    """Signal the embedded-bridge supervisor to exit.

    `supervisor.stop()` does two things atomically: sets the no-respawn
    flag (so the loop will not construct a fresh server on the next
    iteration) and trips the live server's cancellation token so its
    `run()` returns. The token is the same out-of-mutex one (>= 0.2.0rc5)
    that the bridge's own `reconfigure` action uses, so the cleanup
    path runs identically on both the signal and non-signal shutdowns.
    The caller's `await`-on-the-task is the barrier that waits for the
    last iteration's cleanup to finish; no fixed sleep is needed.
    """
    supervisor.stop()


async def _resolve_embedded_bridge(state: State, args: Any) -> tuple[Any, asyncio.Task] | None:
    """Decide whether to spawn an embedded bridge, and if so spawn it.

    Logic mirrored from a single source of truth so unit tests can exercise
    the collision check without invoking the full manager lifecycle:
      - Flag not set → never spawn.
      - Flag set + external bridge already on this root (templates landed
        within 1s) → set `embedded_bridge_aborted` warning, do not spawn.
      - Flag set + no external → spawn.
    Returns (supervisor, task) on spawn, None otherwise.
    """
    # Record the request up front so the UI can flag the conflict case even when
    # we end up aborting the embed below.
    state.embed_requested = bool(args.embed_bridge)
    if not args.embed_bridge:
        return None
    external_present = await state.wait_for(lambda: state.templates is not None, timeout=1.0)
    if external_present:
        msg = (
            f"--embed-bridge requested, but a bridge is already running on "
            f"root '{args.root}'. Stop the external bridge, drop "
            f"--embed-bridge, or pick a different --root."
        )
        await state.set_warning("embedded_bridge_aborted", "error", msg)
        logger.error(msg)
        return None
    embedded = _spawn_embedded_bridge(args)
    state.bridge_embedded = True
    logger.info("Embedded bridge started on root=%r", args.root)
    return embedded


class Manager:
    """Owns the connected, bootstrapped core of a manager session:
    `State` + `BridgeClient`, an optional embedded bridge, and a
    `WizardManager`/`LanScanCoordinator` pair for device onboarding —
    everything `cli.py` and `web.py` need, with no web dependency.

    Two-phase like `BridgeClient` itself: construction only stores config
    (no I/O); `async with` connects, loads the cloud file, and (if
    requested) spawns the embedded bridge.
    """

    def __init__(
        self,
        *,
        cloud_path: str | Path = "tuyadevices.json",
        broker: str | None = None,
        root: str | None = None,
        client_id: str = "rustuya-manager",
        mqtt_user: str | None = None,
        mqtt_pass: str | None = None,
        on_event: EventCallback | None = None,
        embed_bridge: bool = False,
        bridge_state: str | None = None,
        bridge_config: str | None = None,
        log_level: str | None = None,
        creds_path: str | None = None,
    ) -> None:
        self.cloud_path = Path(cloud_path)
        self.broker = broker
        self.root = root
        self.client_id = client_id
        self.mqtt_user = mqtt_user
        self.mqtt_pass = mqtt_pass
        self.on_event = on_event
        self.embed_bridge = embed_bridge
        self.bridge_state = bridge_state
        self.bridge_config = bridge_config
        self.log_level = log_level
        self.creds_path = creds_path

        # Populated by __aenter__; valid only inside the `async with` block.
        self.state: State = None  # type: ignore[assignment]
        self.client: BridgeClient = None  # type: ignore[assignment]
        self.scan_coordinator: LanScanCoordinator = None  # type: ignore[assignment]
        self.wizard: WizardManager = None  # type: ignore[assignment]
        self._embedded_bridge: tuple[_EmbeddedBridgeSupervisor, asyncio.Task] | None = None
        self._exit_stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> Manager:
        async with contextlib.AsyncExitStack() as stack:
            _resolve_mqtt_credentials(self)
            _apply_bridge_config_defaults(self)
            _apply_manager_defaults(self)

            self.state = State()
            await self.state.set_cloud_path(str(self.cloud_path.resolve()))
            if self.cloud_path.exists():
                await self.state.set_cloud(_load_cloud(self.cloud_path))

            self.client = BridgeClient(
                broker=self.broker,
                root=self.root,
                state=self.state,
                client_id=self.client_id,
                on_event=self.on_event,
                username=self.mqtt_user,
                password=self.mqtt_pass,
            )
            await stack.enter_async_context(self.client)

            self._embedded_bridge = await _resolve_embedded_bridge(self.state, self)

            self.scan_coordinator = LanScanCoordinator(self.client, self.state)
            # Resolve and persist so callers can inspect the actual value used
            # (mirrors `cloud_path`, which is always the resolved Path too).
            self.creds_path = self.creds_path or str(self.cloud_path.parent / "tuyacreds.json")
            self.wizard = WizardManager(
                creds_path=self.creds_path,
                on_devices=self._on_wizard_devices,
                scan_coordinator=self.scan_coordinator,
            )

            # Hand ownership to self only once every step above succeeded —
            # if anything raised, `stack`'s own __aexit__ (triggered by this
            # `async with`) unwinds whatever was entered so far (namely
            # `client`) instead of leaking a live MQTT connection.
            self._exit_stack = stack.pop_all()
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Order matches the original cli.py `run()`: the manager's own MQTT
        # connection closes first, then the embedded bridge (if any) is torn
        # down — see _close_embedded_bridge's docstring for why that's a
        # clean shutdown rather than an abrupt one.
        assert self._exit_stack is not None
        await self._exit_stack.aclose()
        if self._embedded_bridge is not None:
            supervisor, task = self._embedded_bridge
            await _close_embedded_bridge(supervisor)
            # 5s headroom over the bridge's graceful MQTT cleanup (broker
            # disconnect + retained-config clear). wait_for cancels the task
            # on timeout, so a wedged cleanup can't hang shutdown.
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("embedded bridge supervisor did not exit within 5s")

    @property
    def embedded_bridge_active(self) -> bool:
        """True once `__aenter__` has spawned an embedded bridge (i.e.
        `embed_bridge=True` and no colliding external bridge was found).
        Always False before entry or when `embed_bridge=False`."""
        return self._embedded_bridge is not None

    async def _on_wizard_devices(self, devices: list[dict[str, Any]]) -> None:
        """Feed devices fetched via the Tuya Cloud login wizard into `state`,
        identically to a JSON upload, and persist them to `cloud_path`."""
        raw = json.dumps(devices, ensure_ascii=False)
        try:
            parsed = parse_cloud_json(raw)
        except CloudFormatError as e:
            logger.warning("wizard returned unparseable device shape: %s", e)
            return
        await self.state.set_cloud(parsed)
        if self.state.cloud_path:
            try:
                save_cloud_json(raw, Path(self.state.cloud_path))
            except OSError as e:
                logger.warning("wizard fetched devices but persist failed: %s", e)

    async def wait_ready(
        self, *, bootstrap_timeout: float = 6.0, bridge_timeout: float = 3.0
    ) -> None:
        """Wait for the MQTT bootstrap handshake and the bridge's initial
        `status` reply. Separate from `__aenter__` so a caller that doesn't
        need full readiness (e.g. firing a single fast command) can skip it."""
        await self.client.wait_bootstrap(timeout=bootstrap_timeout)
        await self.state.wait_for(lambda: bool(self.state.bridge), timeout=bridge_timeout)

    async def publish_command(
        self,
        action: str,
        *,
        target_id: str | None = None,
        target_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Passthrough to `self.client.publish_command` — sugar so callers
        don't need the `.client.` prefix for the common case."""
        await self.client.publish_command(
            action, target_id=target_id, target_name=target_name, extra=extra
        )

    async def add_device(self, target_id: str, **extra: Any) -> None:
        await self.client.publish_command("add", target_id=target_id, extra=extra or None)

    async def remove_device(self, target_id: str, **extra: Any) -> None:
        await self.client.publish_command("remove", target_id=target_id, extra=extra or None)

    async def sync(self) -> DiffResult:
        """Read-only cloud-vs-bridge diff. Reconciling it (issuing the
        add/remove commands the diff implies) stays the caller's job —
        auto-applying a diff is a policy decision this facade shouldn't
        make silently on a library caller's behalf."""
        return self.state.diff()
