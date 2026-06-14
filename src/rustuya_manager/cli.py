"""Step-2 CLI: bootstrap the bridge connection, show diff, and stream events.

This intentionally drops the interactive sync menu from the old single-file
script — that flow is being rebuilt on top of the web backend in Step 4. Once
the web UI is up, the same actions become buttons.

Until then, use the CLI for:
  - verifying bootstrap works against any bridge config (default or custom)
  - watching live DPS events
  - inspecting the cloud-vs-bridge diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pyrustuyabridge as pb

from .diff import DiffResult
from .models import Device
from .mqtt import BridgeClient
from .state import State

logger = logging.getLogger(__name__)

# Defaults are constants rather than inline argparse strings so the
# "did the user override?" check in run() has a stable thing to compare
# against — and so the bridge-config fallback knows which manager-side
# defaults are placeholders worth replacing.
DEFAULT_BROKER = "mqtt://localhost:1883"
DEFAULT_ROOT = "rustuya"


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


def _apply_bridge_config_defaults(args: argparse.Namespace) -> None:
    """If `--bridge-config` carries `mqtt_broker` / `mqtt_root_topic` /
    `state_file`, fill those into args when the user did NOT pass the
    corresponding CLI flag — so the user only has to specify them in one
    place when embedding the bridge.

    Precedence: CLI flag > bridge-config field > manager default. The
    three flags involved here (`--broker`, `--root`, `--bridge-state`) all
    use `default=None` on the parser side so that "user passed the flag"
    can be distinguished from "argparse filled in the default" — without
    that sentinel, a user who explicitly typed `--broker mqtt://localhost:1883`
    (the same string as the manager default) would silently lose to the
    bridge-config value.

    If the user explicitly set a CLI flag AND it disagrees with the
    bridge-config value, a warning is logged because the embedded bridge
    will end up with the kwarg value (manager's CLI) while the
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


def _apply_manager_defaults(args: argparse.Namespace) -> None:
    """Fill any still-`None` sentinel values with the manager's own
    defaults. Runs AFTER `_apply_bridge_config_defaults` so the precedence
    chain `CLI > bridge-config > manager default` resolves bottom-up
    without losing the "user provided?" signal."""
    if args.broker is None:
        args.broker = DEFAULT_BROKER
    if args.root is None:
        args.root = DEFAULT_ROOT


def _resolve_mqtt_credentials(args: argparse.Namespace) -> None:
    """Fill broker credentials from the environment when the flag is absent.

    Precedence: `--mqtt-user`/`--mqtt-pass` > `RUSTUYA_MQTT_USER`/
    `RUSTUYA_MQTT_PASSWORD` env. Prefer the env vars in production — a password
    passed as a CLI flag is visible in the host's process list (`ps`). Runs
    before `_apply_bridge_config_defaults` so the resolved value is what the
    bridge-config precedence check compares against."""
    if args.mqtt_user is None:
        args.mqtt_user = os.environ.get("RUSTUYA_MQTT_USER") or None
    if args.mqtt_pass is None:
        args.mqtt_pass = os.environ.get("RUSTUYA_MQTT_PASSWORD") or None


async def _on_event(
    matched_as: str,
    vars_: dict[str, str],
    parsed: Any,
    extras: dict[str, Any] | None,
) -> None:
    """Log every bridge publish so the gate can observe the full cycle.

    `extras` carries the manager's resolved key + extracted DPS for event
    type — the CLI prints the same DPS map that the web UI renders, not
    whatever the bridge's parse_mqtt_payload happened to leave in `parsed`.

    Retained messages (`extras["retain"]`) are skipped: on a busy broker the
    initial subscribe burst is hundreds of lines of stale state, and the
    diff summary printed after bootstrap already captures the net effect.
    """
    e = extras or {}
    if e.get("retain"):
        return
    if matched_as == "event":
        device = e.get("device_id") or vars_.get("name") or vars_.get("id") or "?"
        print(f"  [event] {device}: {e.get('dps')}")
    elif matched_as == "message":
        level = vars_.get("level", "?")
        target = vars_.get("id", "?")
        action = parsed.get("action") if isinstance(parsed, dict) else None
        status = parsed.get("status") if isinstance(parsed, dict) else None
        print(f"  [{level}] {target} action={action} status={status}")
    elif matched_as == "scanner":
        print(f"  [scanner] {parsed}")


def _load_cloud(path: Path) -> dict[str, Device]:
    with path.open() as f:
        data = json.load(f)
    iterable = data if isinstance(data, list) else data.values()
    return {d["id"]: Device.from_dict(d) for d in iterable if "id" in d}


def _print_diff(diff: DiffResult) -> None:
    # Section order matches the UI / DiffResult.summary order:
    # missing → orphan → mismatch (synced is implicit when none of those fire).
    print(f"\n=== Diff: {diff.summary()} ===")
    if diff.missing:
        print("  MISSING (in cloud, absent from bridge):")
        for dev in diff.missing:
            print(f"    - {dev.id} ({dev.name})")
    if diff.orphaned:
        print("  ORPHANED (in bridge, absent from cloud):")
        for dev in diff.orphaned:
            print(f"    - {dev.id} ({dev.name})")
    if diff.mismatched:
        print("  MISMATCH:")
        for dev, reasons in diff.mismatched:
            print(f"    - {dev.id} ({dev.name}): {'; '.join(reasons)}")
    if not diff.has_changes:
        # No-changes line lives at column 0 (like `✓ Bootstrap complete`)
        # rather than indented as a "detail" of a section that doesn't
        # exist — the alignment with sibling status messages reads cleaner.
        print("✓ Bridge and cloud match.")
    print()


async def _serve_web(host: str, port: int, app: Any) -> None:
    """Run uvicorn programmatically alongside the MQTT loop (same event loop)."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()


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

    Thread safety:
      `stop()` may be called from any thread (typically the asyncio
      shutdown path). A `threading.Lock` serialises the read of the
      current server reference against the loop replacing it between
      iterations. PyBridgeServer's `stop()` itself is lock-free
      (out-of-mutex cancellation token, §1.3), so the supervisor's
      lock only protects the Python reference — not the bridge state.
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
        self._stop = threading.Event()
        self._server: Any = None
        self._lock = threading.Lock()
        self._restart_count = 0  # public via the .restart_count attribute

    @property
    def restart_count(self) -> int:
        """Number of times the bridge has been respawned since the
        supervisor started. Includes both reconfigure-driven and
        crash-driven restarts; counted AFTER the spawn completes."""
        return self._restart_count

    def run(self) -> None:
        """Daemon-thread entry. Returns when `stop()` has been called
        or the rate limit has been exceeded."""
        exits: list[float] = []
        while not self._stop.is_set():
            crashed = False
            try:
                with self._lock:
                    if self._stop.is_set():
                        return
                    self._server = pb.PyBridgeServer(**self._kwargs)
                self._server.start()
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
                # Event.wait() returns True if .set() was called during
                # the timeout — that lets stop() interrupt the backoff
                # for a fast shutdown instead of always waiting the
                # full 5 seconds.
                if self._stop.wait(timeout=self._CRASH_BACKOFF_SEC):
                    return
            else:
                logger.info(
                    "embedded bridge exited cleanly (reconfigure or self-terminate); respawning"
                )

    def stop(self) -> None:
        """Signal the supervisor to exit. Trips the live server's
        cancellation token (if any) and prevents the loop from
        starting a fresh one. Idempotent and safe from any thread."""
        self._stop.set()
        with self._lock:
            srv = self._server
        if srv is None:
            return
        try:
            srv.stop()
        except Exception:  # noqa: BLE001 - best-effort; the loop will exit anyway
            logger.exception("stop() on the embedded bridge failed; supervisor will exit anyway")


def _spawn_embedded_bridge(
    args: argparse.Namespace,
) -> tuple[_EmbeddedBridgeSupervisor, threading.Thread]:
    """Build the embedded-bridge supervisor and start its daemon thread.

    The supervisor (see `_EmbeddedBridgeSupervisor`) owns the per-iteration
    `PyBridgeServer` lifecycle so the bridge's `reconfigure` action — which
    self-terminates the bridge to apply a fresh config — gets a fresh
    process equivalent in-process. Returns `(supervisor, thread)`; the
    caller drives shutdown via `supervisor.stop()` + `thread.join(...)`.
    """
    default_state = Path(args.cloud).resolve().parent / "rustuya.json"
    state_file = args.bridge_state or str(default_state)

    # The manager owns broker / root / state-file / log-level / broker creds —
    # the embedded bridge must agree with the manager's view on those, since
    # both connect to the same broker. Broker credentials are co-owned now (the
    # manager has its own authenticated connection): they come from
    # --mqtt-user/--mqtt-pass or the env, and are forwarded as kwargs here so a
    # single-process deploy configures them once. Everything else (custom
    # topics, scanner options, retain flag, …) stays the bridge's domain, read
    # from `--bridge-config` when provided. pyrustuyabridge resolves
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
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    return supervisor, thread


async def _close_embedded_bridge(supervisor: _EmbeddedBridgeSupervisor) -> None:
    """Signal the embedded-bridge supervisor to exit.

    `supervisor.stop()` does two things atomically: sets the no-respawn
    flag (so the loop will not construct a fresh server on the next
    iteration) and trips the live server's cancellation token so its
    `run()` returns. The token is the same out-of-mutex one (>= 0.2.0rc5)
    that the bridge's own `reconfigure` action uses, so the cleanup
    path runs identically on both the signal and non-signal shutdowns.
    The caller's `thread.join()` is the barrier that waits for the
    last iteration's cleanup to finish; no fixed sleep is needed.
    """
    supervisor.stop()


async def _resolve_embedded_bridge(
    state: State, args: argparse.Namespace
) -> tuple[Any, threading.Thread] | None:
    """Decide whether to spawn an embedded bridge, and if so spawn it.

    Logic mirrored from a single source of truth so unit tests can exercise
    the collision check without invoking the full CLI loop:
      - Flag not set → never spawn.
      - Flag set + external bridge already on this root (templates landed
        within 1s) → set `embedded_bridge_aborted` warning, do not spawn.
      - Flag set + no external → spawn.
    Returns (server, thread) on spawn, None otherwise.
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


def _web_urls(host: str, port: int) -> list[str]:
    """URLs to print at startup so the user can click straight from the terminal.

    Most modern terminals auto-detect bare http:// URLs as clickable. We bias
    toward the URLs that will actually work: when bound to 0.0.0.0, the
    host's LAN IPs are reachable from other machines; when bound to a
    specific address, only that one is shown.
    """
    import socket

    port_s = str(port)
    if host == "0.0.0.0":
        urls = [f"http://localhost:{port_s}/"]
        # Best-effort LAN discovery via the kernel's chosen outbound interface.
        # Failures are fine — we still printed localhost.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                lan_ip = probe.getsockname()[0]
            if lan_ip and not lan_ip.startswith("127."):
                urls.append(f"http://{lan_ip}:{port_s}/")
        except OSError:
            pass
        hostname = socket.gethostname()
        if hostname and hostname not in ("localhost", "127.0.0.1"):
            urls.append(f"http://{hostname}:{port_s}/")
        return urls
    if host in ("127.0.0.1", "localhost", "::1"):
        return [f"http://localhost:{port_s}/"]
    return [f"http://{host}:{port_s}/"]


async def run(args: argparse.Namespace) -> int:
    # When stdout is redirected to a pipe/file, Python block-buffers it; logger
    # flushes per record but `print()` doesn't. Make stdout line-buffered so the
    # live event stream appears as it happens regardless of redirect.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass  # Python < 3.7 — unsupported here, ignore.

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # If --embed-bridge + --bridge-config, let the JSON's mqtt_broker /
    # mqtt_root_topic supply manager defaults so the user doesn't have to
    # repeat the same values twice. Must run BEFORE BridgeClient is built
    # since that fixes the broker/root for the manager's own MQTT
    # connection. CLI-given values always win.
    _resolve_mqtt_credentials(args)
    _apply_bridge_config_defaults(args)
    _apply_manager_defaults(args)

    state = State()
    cloud_path = Path(args.cloud)
    # Remember where to persist later uploads, even if the file doesn't exist yet.
    await state.set_cloud_path(str(cloud_path.resolve()))
    if cloud_path.exists():
        cloud = _load_cloud(cloud_path)
        await state.set_cloud(cloud)
        print(f"Loaded {len(cloud)} cloud devices from {cloud_path}")
    else:
        print(f"NOTE: cloud file {cloud_path} not found — bridge devices will show as 'ungrouped'.")
        print("      Upload tuyadevices.json via the web UI to enable diff/sync.")

    # Quiet-mode for the event callback when running with --web: stdout becomes
    # uvicorn's territory, so don't interleave per-event prints there.
    event_cb = None if args.web else _on_event

    client = BridgeClient(
        broker=args.broker,
        root=args.root,
        state=state,
        client_id=args.client_id,
        on_event=event_cb,
        username=args.mqtt_user,
        password=args.mqtt_pass,
    )

    print(f"Connecting to {args.broker}, root={args.root!r} ...")

    # Wire SIGINT/SIGTERM into a clean shutdown.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    embedded_bridge = None
    try:
        async with client:
            # --embed-bridge handling — collision detection + spawn (see helper).
            embedded_bridge = await _resolve_embedded_bridge(state, args)
            if args.embed_bridge:
                if embedded_bridge is None:
                    # Refused due to existing external — the helper already
                    # logged + set the state warning; mirror it to stdout for
                    # CLI users.
                    warn = state.warnings.get("embedded_bridge_aborted")
                    if warn:
                        print(f"⚠ {warn['message']}")
                else:
                    print(f"✓ Embedded bridge running on root={args.root!r}")

            # Wait for bootstrap with 6s slack over the client's internal 5s
            # fallback. wait_bootstrap returns silently on timeout; we then
            # use state.warnings (the same signal the UI uses) to decide
            # which message to print.
            await client.wait_bootstrap(timeout=6.0)
            if client._bootstrap_done.is_set() and "bridge_offline" not in state.warnings:
                print("✓ Bootstrap complete")
            elif "broker_unreachable" in state.warnings:
                print(
                    "⚠ Broker still unreachable — manager will keep retrying. "
                    "Watch state warnings for status."
                )
            else:
                print("⚠ Bootstrap timeout — bridge may be offline; using defaults")

            # Wait for the bridge's initial `status` reply to land (which
            # populates state.bridge). Bounded so we still print *something*
            # if the bridge never replies.
            await state.wait_for(lambda: bool(state.bridge), timeout=3.0)
            # Skip the diff dump when there's no cloud — without a reference
            # set, every bridge device would land in "ORPHANED" which
            # contradicts the "showing as ungrouped" NOTE printed at startup.
            if state.cloud:
                _print_diff(state.diff())
            else:
                print(
                    f"\n=== Bridge: {len(state.bridge)} device(s) "
                    f"(no cloud loaded — diff skipped) ===\n"
                )

            if args.web:
                from .web import build_app

                creds_path = args.creds or str(cloud_path.parent / "tuyacreds.json")
                app = build_app(state, client, creds_path=creds_path, auth=args.auth)
                for url in _web_urls(args.host, args.port):
                    print(f"Serving web UI on {url}")
                if args.auth:
                    print(f"  (HTTP Basic auth enabled — user '{args.auth.split(':', 1)[0]}')")
                web_task = asyncio.create_task(_serve_web(args.host, args.port, app))
                try:
                    await stop_event.wait()
                finally:
                    print("\nShutting down ...")
                    web_task.cancel()
                    await asyncio.gather(web_task, return_exceptions=True)
            else:
                print(
                    f"Watching for events. Press Ctrl+C to exit. "
                    f"(bridge has {len(state.bridge)} devices)"
                )
                await stop_event.wait()
                print("\nShutting down ...")
        # `async with client` exited — reconnect task cancelled, aiomqtt
        # context closed cleanly.
    finally:
        # Embedded bridge tear-down — runs even on exception so a manager
        # crash doesn't leave a retained bridge/config behind.
        if embedded_bridge is not None:
            server, thread = embedded_bridge
            await _close_embedded_bridge(server)
            # 5s headroom over the bridge's graceful MQTT cleanup (broker
            # disconnect + retained-config clear) which runs inside run()
            # once stop() trips the token. Local cleanup is ~ms; the slack
            # is for a slow/remote broker.
            thread.join(timeout=5)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rustuya-manager")
    parser.add_argument("-l", "--cloud", default="tuyadevices.json", help="Path to Tuya Cloud JSON")
    parser.add_argument(
        "-b",
        "--broker",
        default=None,
        help=(
            f"MQTT broker URL. Use mqtt://host:port for plaintext or "
            f"mqtts://host:port for TLS (validated against the system trust store; "
            f"default port 8883). Default {DEFAULT_BROKER!r} is applied when the "
            "flag is absent AND no bridge-config supplies one — leaving the flag "
            "off is how the bridge-config fallback (--bridge-config) is allowed to win."
        ),
    )
    parser.add_argument(
        "-r",
        "--root",
        default=None,
        help=(
            f"MQTT root topic (must match the running bridge). Default {DEFAULT_ROOT!r} "
            "is applied when the flag is absent AND no bridge-config supplies one."
        ),
    )
    parser.add_argument("--client-id", default="rustuya-manager")
    parser.add_argument(
        "--mqtt-user",
        default=None,
        help=(
            "MQTT broker username for the manager's own connection (and the "
            "embedded bridge under --embed-bridge). Falls back to the "
            "RUSTUYA_MQTT_USER env var. Required by most hosted/TLS brokers."
        ),
    )
    parser.add_argument(
        "--mqtt-pass",
        default=None,
        metavar="PASSWORD",
        help=(
            "MQTT broker password. Falls back to the RUSTUYA_MQTT_PASSWORD env "
            "var — prefer the env var: a password on the command line is visible "
            "in the host process list (ps). Use a TLS broker URL (mqtts://...) "
            "so credentials aren't sent in the clear."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the FastAPI web server alongside the MQTT loop",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Web server bind address (--web only). Defaults to 127.0.0.1 so the "
            "UI is not exposed beyond localhost unless explicitly opened. Use "
            "0.0.0.0 to bind on every interface (pair with --auth)."
        ),
    )
    parser.add_argument("--port", type=int, default=8373, help="Web server port (--web only)")
    parser.add_argument(
        "--auth",
        default=None,
        metavar="USER:PASS",
        help=(
            "Enable HTTP Basic auth for the web UI. Format: 'user:password' "
            "(plain text — credentials never leave the manager process). "
            "Strongly recommended whenever --host is not 127.0.0.1."
        ),
    )
    parser.add_argument(
        "--creds",
        default=None,
        help="Path to tuyacreds.json (tuyawizard's session cache). "
        "Default: tuyacreds.json next to the cloud file.",
    )
    parser.add_argument(
        "--embed-bridge",
        action="store_true",
        help=(
            "Run the rustuya-bridge inside this manager process via the "
            "pyrustuyabridge bindings. Useful for single-process deploys "
            "(pipx install + run). Refused at startup with a clear warning "
            "if another bridge is already publishing on --root."
        ),
    )
    parser.add_argument(
        "--bridge-state",
        default=None,
        help=(
            "Path to the embedded bridge's state file (--embed-bridge only). "
            "Default: rustuya.json in the same directory as the cloud "
            "file (matches the standalone bridge's DEFAULT_STATE_FILE)."
        ),
    )
    parser.add_argument(
        "--bridge-config",
        default=None,
        help=(
            "Path to a JSON config file for the embedded bridge (--embed-bridge "
            "only). Same format as rustuya-bridge's --config: existing file is "
            "read and merged (manager flags still win), missing file is "
            "auto-created from the merged settings. Lets you set custom topic "
            "templates, MQTT auth, scanner options etc. without re-exposing "
            "every bridge flag here."
        ),
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
