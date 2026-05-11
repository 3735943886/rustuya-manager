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
from pathlib import Path
from typing import Any

from .diff import DiffResult
from .models import Device
from .mqtt import BridgeClient
from .state import State

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
    """
    if matched_as == "event":
        e = extras or {}
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
    print(f"\n=== Diff: {diff.summary()} ===")
    if diff.mismatched:
        print("  MISMATCH:")
        for dev, reasons in diff.mismatched:
            print(f"    - {dev.id} ({dev.name}): {'; '.join(reasons)}")
    if diff.missing:
        print("  MISSING (in cloud, absent from bridge):")
        for dev in diff.missing:
            print(f"    - {dev.id} ({dev.name})")
    if diff.orphaned:
        print("  ORPHANED (in bridge, absent from cloud):")
        for dev in diff.orphaned:
            print(f"    - {dev.id} ({dev.name})")
    if not diff.has_changes:
        print("  ✓ Bridge and cloud match.")
    print()


async def _serve_web(host: str, port: int, app: Any) -> None:
    """Run uvicorn programmatically alongside the MQTT loop (same event loop)."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()


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
        print(f"      Upload tuyadevices.json via the web UI to enable diff/sync.")

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

    # Wait either for bootstrap or 6s — give a bit of slack over the client's 5s.
    try:
        await asyncio.wait_for(client._bootstrap_done.wait(), 6.0)
        print("✓ Bootstrap complete")
    except asyncio.TimeoutError:
        print("⚠ Bootstrap timeout — bridge may be offline; using defaults")

    # Wait for the bridge's initial `status` reply to land (it bumps state
    # version when it arrives). Bounded so we still print *something* even
    # if the reply never comes.
    bootstrap_version = state.version
    try:
        await asyncio.wait_for(state.wait_for_change(bootstrap_version), 3.0)
    except asyncio.TimeoutError:
        pass
    _print_diff(state.diff())

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
        app = build_app(state, client, creds_path=creds_path)
        print(f"Serving web UI on http://{args.host}:{args.port}")
        web_task = asyncio.create_task(_serve_web(args.host, args.port, app))
        # When the user hits Ctrl+C, stop both web and MQTT tasks.
        await stop_event.wait()
        print("\nShutting down ...")
        web_task.cancel()
        await client.stop()
        await asyncio.gather(web_task, run_task, return_exceptions=True)
    else:
        print(f"Watching for events. Press Ctrl+C to exit. (bridge has {len(state.bridge)} devices)")
        await stop_event.wait()
        print("\nShutting down ...")
        await client.stop()
        await run_task
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rustuya-manager")
    parser.add_argument(
        "-l", "--cloud", default="tuyadevices.json", help="Path to Tuya Cloud JSON"
    )
    parser.add_argument(
        "-b",
        "--broker",
        default="mqtt://localhost:1883",
        help="MQTT broker URL (mqtt://host:port)",
    )
    parser.add_argument(
        "-r",
        "--root",
        default="rustuya",
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
        "--host", default="0.0.0.0", help="Web server host (--web only)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Web server port (--web only)"
    )
    parser.add_argument(
        "--creds",
        default=None,
        help="Path to tuyacreds.json (tuyawizard's session cache). "
             "Default: tuyacreds.json next to the cloud file.",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
