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
import logging
import os
import signal
import sys
from typing import Any

from .diff import DiffResult
from .manager import DEFAULT_BROKER, DEFAULT_ROOT, Manager

logger = logging.getLogger(__name__)


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
    """Run uvicorn programmatically alongside the MQTT loop (same event loop).

    Design note: this stays CLI-side rather than moving to web.py — it's
    about *how this process* serves the app (uvicorn config, signal-handler
    suppression tied to the CLI's own SIGINT/SIGTERM ownership), not about
    what the app *is*. A different embedder (e.g. serving build_app() under
    gunicorn) would not reuse this helper anyway.
    """
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    # The manager owns SIGINT/SIGTERM (loop.add_signal_handler → stop_event, set
    # up in run()); the embedded-bridge no-signals design depends on that single
    # owner. uvicorn.Server.serve() otherwise installs its own handlers and
    # overrides ours — so disable its capture and let the manager's handlers
    # drive shutdown.
    server.install_signal_handlers = lambda: None
    await server.serve()


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

    # Quiet-mode for the event callback when running with --web: stdout becomes
    # uvicorn's territory, so don't interleave per-event prints there.
    event_cb = None if args.web else _on_event

    manager = Manager(
        cloud_path=args.cloud,
        broker=args.broker,
        root=args.root,
        client_id=args.client_id,
        mqtt_user=args.mqtt_user,
        mqtt_pass=args.mqtt_pass,
        on_event=event_cb,
        embed_bridge=args.embed_bridge,
        bridge_state=args.bridge_state,
        bridge_config=args.bridge_config,
        log_level=args.log_level,
        creds_path=args.creds,
    )

    # Wire SIGINT/SIGTERM into a clean shutdown.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    async with manager:
        state = manager.state
        if state.cloud:
            print(f"Loaded {len(state.cloud)} cloud devices from {manager.cloud_path}")
        else:
            print(
                f"NOTE: cloud file {manager.cloud_path} not found — bridge devices "
                "will show as 'ungrouped'."
            )
            print("      Upload tuyadevices.json via the web UI to enable diff/sync.")

        print(f"Connecting to {manager.broker}, root={manager.root!r} ...")

        # --embed-bridge handling — collision detection + spawn already ran
        # inside `async with manager:`; just report the outcome.
        if args.embed_bridge:
            if not manager.embedded_bridge_active:
                # Refused due to existing external — Manager already logged +
                # set the state warning; mirror it to stdout for CLI users.
                warn = state.warnings.get("embedded_bridge_aborted")
                if warn:
                    print(f"⚠ {warn['message']}")
            else:
                print(f"✓ Embedded bridge running on root={manager.root!r}")

        # Wait for bootstrap with 6s slack over the client's internal 5s
        # fallback. wait_bootstrap returns silently on timeout; we then
        # use state.warnings (the same signal the UI uses) to decide
        # which message to print.
        await manager.wait_ready(bootstrap_timeout=6.0, bridge_timeout=3.0)
        if manager.client._bootstrap_done.is_set() and "bridge_offline" not in state.warnings:
            print("✓ Bootstrap complete")
        elif "broker_unreachable" in state.warnings:
            print(
                "⚠ Broker still unreachable — manager will keep retrying. "
                "Watch state warnings for status."
            )
        else:
            print("⚠ Bootstrap timeout — bridge may be offline; using defaults")

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

            # The managed plugin dir is both the drop-in scan target and where
            # the in-UI catalog installs plugins, so it's always set (unlike the
            # old opt-in behaviour): an explicit --plugin-dir / env relocates it,
            # otherwise it defaults next to the cloud file. The dir is created
            # lazily on first install; build_app skips scanning it until it exists.
            managed_plugin_dir = (
                args.plugin_dir
                or os.environ.get("RUSTUYA_MANAGER_PLUGIN_DIR")
                or str(manager.cloud_path.parent / "plugins")
            )
            app = build_app(
                state,
                manager.client,
                auth=args.auth,
                managed_plugin_dir=managed_plugin_dir,
                wizard=manager.wizard,
                scan_coordinator=manager.scan_coordinator,
            )
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
        # `async with manager:` below exits here — reconnect task cancelled,
        # aiomqtt context closed, then embedded bridge (if any) torn down.

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
        "--plugin-dir",
        default=None,
        metavar="DIR",
        help=(
            "The managed plugin directory (--web only): drop-in plugins here are "
            "loaded without pip install, AND it's where the in-UI catalog installs "
            "plugins. Each child that is a package (has __init__.py) or a top-level "
            ".py file exposing register(ctx) is loaded, alongside any pip-installed "
            "entry-point plugins. Default: a 'plugins' folder next to the cloud "
            "file; for Docker, point it at a mounted volume e.g. /data/plugins. Env "
            "fallback: RUSTUYA_MANAGER_PLUGIN_DIR. Note: drop-in plugins can't "
            "install their own dependencies, and loading code from this dir executes "
            "it in-process — only install plugins you trust."
        ),
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
