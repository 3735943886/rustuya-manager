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
import signal
import sys
import threading
from pathlib import Path
from typing import Any

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
    """If `--bridge-config` carries `mqtt_broker` / `mqtt_root_topic`, treat
    them as the manager's defaults too — so the user only has to specify
    them in one place when embedding the bridge.

    Precedence: CLI flag > bridge-config field > manager default. A CLI
    flag at the manager-default value is treated as "not set" for this
    fallback. If the user explicitly set the CLI flag AND it disagrees with
    the bridge-config value, a warning is logged because the embedded
    bridge will end up with the kwarg value (manager's CLI) while the
    bridge-config file says something else — confusing on disk-diff.
    """
    if not args.embed_bridge or not args.bridge_config:
        return
    cfg = _peek_bridge_config(args.bridge_config)
    cfg_broker = cfg.get("mqtt_broker")
    cfg_root = cfg.get("mqtt_root_topic")

    if cfg_broker:
        if args.broker == DEFAULT_BROKER:
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
        if args.root == DEFAULT_ROOT:
            args.root = cfg_root
            logger.info("Using root %r from --bridge-config", cfg_root)
        elif args.root != cfg_root:
            logger.warning(
                "--root (%r) disagrees with --bridge-config mqtt_root_topic (%r); "
                "manager will use the CLI value, embedded bridge will follow.",
                args.root,
                cfg_root,
            )


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
        print("  ✓ Bridge and cloud match.")
    print()


async def _serve_web(host: str, port: int, app: Any) -> None:
    """Run uvicorn programmatically alongside the MQTT loop (same event loop)."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()


def _spawn_embedded_bridge(args: argparse.Namespace) -> tuple[Any, threading.Thread]:
    """Spin up a `pyrustuyabridge.PyBridgeServer` in a daemon thread.

    Used when `--embed-bridge` is set AND no external bridge has claimed the
    root topic yet (collision check happens in `run()` before this is called).
    Returns (server, thread) so the caller can `server.close()` + join on
    shutdown. The thread is daemon so a manager crash never leaves the
    embedded bridge orphaned.
    """
    import pyrustuyabridge as pb  # imported lazily — only needed when embedding

    default_state = Path(args.cloud).resolve().parent / "bridge-state.json"
    state_file = args.bridge_state or str(default_state)

    # The manager owns broker / root / state-file / log-level — embedded bridge
    # must agree with the manager's view on those. Everything else (custom
    # topics, mqtt user/password, scanner options, retain flag, …) is the
    # bridge's domain and is read from `--bridge-config` when provided.
    # pyrustuyabridge >= 0.1.1 reads + auto-creates that file in PyBridgeServer
    # the same way the binary's `--config` flag does.
    kwargs: dict[str, Any] = {
        "mqtt_broker": args.broker,
        "mqtt_root_topic": args.root,
        "state_file": state_file,
        "log_level": args.log_level,
    }
    if getattr(args, "bridge_config", None):
        kwargs["config_path"] = args.bridge_config

    server = pb.PyBridgeServer(**kwargs)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    return server, thread


async def _close_embedded_bridge(server: Any) -> None:
    """Wrap `server.close()` in an asyncio loop — the bridge's tokio runtime
    expects to schedule cleanup work on one before shutting down."""
    server.close()
    # Tiny grace period so the bridge's cleanup futures can complete before
    # the calling loop tears down underneath them.
    await asyncio.sleep(0.1)


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
    _apply_bridge_config_defaults(args)

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
    )

    print(f"Connecting to {args.broker}, root={args.root!r} ...")
    run_task = asyncio.create_task(client.run())

    # --embed-bridge handling — collision detection + spawn (see helper).
    embedded_bridge = await _resolve_embedded_bridge(state, args)
    if args.embed_bridge:
        if embedded_bridge is None:
            # Refused due to existing external — the helper already logged
            # + set the state warning; mirror it to stdout for CLI users.
            warn = state.warnings.get("embedded_bridge_aborted")
            if warn:
                print(f"⚠ {warn['message']}")
        else:
            print(f"✓ Embedded bridge running on root={args.root!r}")

    # Wait either for bootstrap or 6s — give a bit of slack over the client's 5s.
    try:
        await asyncio.wait_for(client._bootstrap_done.wait(), 6.0)
        print("✓ Bootstrap complete")
    except asyncio.TimeoutError:
        # Distinguish "still retrying broker" from "broker OK but no bridge".
        # state.warnings is the same signal the UI uses, so the CLI matches.
        if "broker_unreachable" in state.warnings:
            print(
                "⚠ Broker still unreachable — manager will keep retrying. "
                "Watch state warnings for status."
            )
        else:
            print("⚠ Bootstrap timeout — bridge may be offline; using defaults")

    # Wait for the bridge's initial `status` reply to land (which populates
    # state.bridge). A naive "wait for any state change" wakes up on retained
    # events that arrive first when mqtt_retain=true is set; here we wait for
    # the specific semantic condition. Bounded so we still print *something*
    # if the bridge never replies.
    await state.wait_for(lambda: bool(state.bridge), timeout=3.0)
    # Skip the diff dump when there's no cloud — without a reference set,
    # every bridge device would land in "ORPHANED" which contradicts the
    # "showing as ungrouped" NOTE printed at startup.
    if state.cloud:
        _print_diff(state.diff())
    else:
        print(f"\n=== Bridge: {len(state.bridge)} device(s) (no cloud loaded — diff skipped) ===\n")

    # Wire SIGINT/SIGTERM into a clean shutdown.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    if args.web:
        from .web import build_app

        creds_path = args.creds or str(cloud_path.parent / "tuyacreds.json")
        app = build_app(state, client, creds_path=creds_path, auth=args.auth)
        for url in _web_urls(args.host, args.port):
            print(f"Serving web UI on {url}")
        if args.auth:
            print(f"  (HTTP Basic auth enabled — user '{args.auth.split(':', 1)[0]}')")
        web_task = asyncio.create_task(_serve_web(args.host, args.port, app))
        # When the user hits Ctrl+C, stop both web and MQTT tasks.
        await stop_event.wait()
        print("\nShutting down ...")
        web_task.cancel()
        await client.stop()
        await asyncio.gather(web_task, run_task, return_exceptions=True)
    else:
        print(
            f"Watching for events. Press Ctrl+C to exit. (bridge has {len(state.bridge)} devices)"
        )
        await stop_event.wait()
        print("\nShutting down ...")
        await client.stop()
        await run_task

    # Embedded bridge tear-down — only reached on clean shutdown of the
    # manager. Daemon thread would die with the process anyway, but
    # close() gives the bridge a chance to clear its retained config and
    # release MQTT cleanly.
    if embedded_bridge is not None:
        server, thread = embedded_bridge
        await _close_embedded_bridge(server)
        thread.join(timeout=3)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rustuya-manager")
    parser.add_argument("-l", "--cloud", default="tuyadevices.json", help="Path to Tuya Cloud JSON")
    parser.add_argument(
        "-b",
        "--broker",
        default=DEFAULT_BROKER,
        help="MQTT broker URL (mqtt://host:port)",
    )
    parser.add_argument(
        "-r",
        "--root",
        default=DEFAULT_ROOT,
        help="MQTT root topic (must match the running bridge)",
    )
    parser.add_argument("--client-id", default="rustuya-manager")
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
    parser.add_argument("--port", type=int, default=8080, help="Web server port (--web only)")
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
            "Default: bridge-state.json next to the cloud file."
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
