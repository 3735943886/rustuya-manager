"""Async wrapper around `tuyawizard` for the web UI's "Connect to Tuya Cloud"
flow.

`tuyawizard.TuyaWizard` is sync (blocking polls, requests). We expose it to
FastAPI by:

1. Holding a single in-memory `WizardSession` (the manager process is
   single-user; concurrent sessions would just collide on tuyacreds.json).
2. Running the blocking calls in `loop.run_in_executor` so the API stays
   responsive.
3. Letting the frontend poll a GET endpoint that returns the session's
   current state + QR image data URL.

State machine:

    IDLE ─► REQUESTING_QR ─► AWAITING_SCAN ─► LOGGED_IN ─► FETCHING ─► DONE
                                  │
                                  └──────────────► ERROR (anywhere)

Once DONE/ERROR, a fresh `start()` resets and begins a new session.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import qrcode
import qrcode.image.svg
from tuyawizard import TuyaWizard
from tuyawizard.wizard import postprocess_devices

if TYPE_CHECKING:
    from .scan import LanScanCoordinator

logger = logging.getLogger(__name__)

QR_POLL_TIMEOUT_SEC = 120
QR_POLL_RETRY_SEC = 5


class WizardState(str, Enum):
    """Possible session states. Inheriting from `str` makes them JSON-friendly."""

    IDLE = "idle"
    REQUESTING_QR = "requesting_qr"
    AWAITING_SCAN = "awaiting_scan"
    LOGGED_IN = "logged_in"
    FETCHING = "fetching"
    DONE = "done"
    ERROR = "error"


@dataclass
class WizardSession:
    state: WizardState = WizardState.IDLE
    qr_url: str | None = None  # raw tuyaSmart-- deep link
    qr_image_data_url: str | None = None  # `data:image/png;base64,...`
    devices_count: int = 0
    message: str = ""
    error: str | None = None
    # Non-fatal info to surface as a toast at completion — used when the
    # session ran to DONE but degraded somewhere (e.g. scan was requested
    # but the bridge wasn't connected so we fell back to parent-only).
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "qr_url": self.qr_url,
            "qr_image_data_url": self.qr_image_data_url,
            "devices_count": self.devices_count,
            "message": self.message,
            "error": self.error,
            "warning": self.warning,
        }


DevicesCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]


class WizardManager:
    """Coordinates one wizard session at a time.

    Construct with `creds_path` (where `tuyacreds.json` lives) and an
    optional `on_devices` async callback fired with the device list after
    a successful fetch. The callback is where the caller integrates the
    fetched devices into application state."""

    def __init__(
        self,
        creds_path: str,
        on_devices: DevicesCallback | None = None,
        scan_coordinator: LanScanCoordinator | None = None,
    ):
        self.creds_path = creds_path
        self._on_devices = on_devices
        # Single entry point for "ask the bridge to UDP-scan the LAN";
        # shared with the Scan button so the two paths can't double-
        # broadcast. When absent (no bridge wired at all), scan=True
        # degrades to parent-only with a session.warning the UI can toast.
        self._scan_coordinator = scan_coordinator
        self.session = WizardSession()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self, user_code: str | None = None, scan: bool = False) -> WizardSession:
        """Begin a new session. If one is already running, returns its current
        state without restarting.

        `scan` chooses postprocess mode: False → "parent" only (link
        sub-devices to their gateway), True → "all" (parent + UDP scan to
        bake current LAN IP into the device record). Default is False
        because baking an IP makes DHCP changes invisible to the bridge —
        the bridge can scan on its own at runtime when no IP is present.
        """
        async with self._lock:
            if self._task and not self._task.done():
                return self.session
            self.session = WizardSession(state=WizardState.REQUESTING_QR)
            self._task = asyncio.create_task(self._run(user_code, scan))
        return self.session

    async def cancel(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self.session.state not in (WizardState.DONE, WizardState.ERROR):
                self.session.state = WizardState.IDLE
                self.session.message = "cancelled"

    def read_saved_user_code(self) -> str | None:
        """Return the user_code persisted in tuyacreds.json, if any.

        The tuyawizard library strips `user_code` from the dict when it loads
        the file (treating it as per-session input), but it leaves the value
        on disk after a successful login. We read it directly so the web UI
        can pre-fill the wizard input — saving the user from re-typing it on
        every new browser / re-fetch attempt.

        Returns None if the file is missing, unreadable, or has no user_code.
        """
        if not self.creds_path or not os.path.exists(self.creds_path):
            return None
        try:
            with open(self.creds_path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read user_code from %s: %s", self.creds_path, e)
            return None
        code = info.get("user_code") if isinstance(info, dict) else None
        return code if isinstance(code, str) and code else None

    async def _run(self, user_code: str | None, scan: bool) -> None:
        """The blocking wizard flow, broken into thread-pool calls so the
        event loop stays responsive.

        Uses `tuyawizard.login_auto` which:
          - tries saved tokens in tuyacreds.json first (no QR, no user_code)
          - falls back to QR login if saved tokens are stale/missing
        `qr_callback` only fires on the QR fallback path, so re-fetches with
        valid saved credentials skip AWAITING_SCAN entirely.
        """
        loop = asyncio.get_running_loop()
        wizard = TuyaWizard(info_file=self.creds_path, logger=logger)

        def qr_callback(qr_url: str | None) -> None:
            # Called from tuyawizard's thread — qr_url is set when the QR is
            # ready to be scanned, then called again with None when the scan
            # completes (so login proceeds). We only act on the show-QR call.
            if qr_url is None:
                return
            self.session.qr_url = qr_url
            self.session.qr_image_data_url = _qr_to_data_url(qr_url)
            self.session.state = WizardState.AWAITING_SCAN
            self.session.message = "Scan the QR code with Smart Life or Tuya Smart app"

        scan_task: asyncio.Task[list[dict[str, Any]]] | None = None
        try:
            self.session.message = "Connecting to Tuya…"
            # login_auto signature: (user_code, creds, qr_callback)
            ok = await loop.run_in_executor(
                None, wizard.login_auto, user_code or None, None, qr_callback
            )
            if not ok:
                # Distinguish "QR scan timed out" (qr_callback fired at least
                # once so qr_image_data_url is set) from "missing user_code on
                # fresh login" (qr_callback never fired).
                qr_was_shown = self.session.qr_image_data_url is not None
                self.session.state = WizardState.ERROR
                if qr_was_shown:
                    self.session.error = "Login was not completed in time. Try again."
                else:
                    self.session.error = (
                        "Login failed. If this is the first time, paste the "
                        "User Code from Smart Life → Me → Settings → Account "
                        "and Security."
                    )
                return

            self.session.state = WizardState.LOGGED_IN
            self.session.message = "Logged in. Fetching devices..."

            # Kick off the bridge LAN scan in parallel with fetch_devices —
            # cloud fetch is the slow part (multi-second round-trip to
            # Tuya's API), the scanner runs in the bridge with its own 18s
            # timeout, and we'll await it once fetch returns. Without the
            # coordinator we fall through to mode="parent" and surface a
            # warning so the UI can toast "scan skipped" at done.
            if scan:
                if self._scan_coordinator is not None:
                    scan_task = asyncio.create_task(self._scan_coordinator.run())
                else:
                    logger.warning(
                        "scan requested but no LanScanCoordinator available — "
                        "falling back to parent-only postprocess"
                    )
                    self.session.warning = (
                        "Bridge not connected — scan skipped, parent linking only."
                    )

            self.session.state = WizardState.FETCHING
            devices = await loop.run_in_executor(None, wizard.fetch_devices)
            self.session.devices_count = len(devices)
            self.session.message = f"Fetched {len(devices)} devices"

            # Resolve the parallel scan. On any scan failure we degrade to
            # parent-only with a session.warning rather than failing the
            # whole flow — the user still gets their devices, just without
            # baked-in IPs.
            scan_results: list[dict[str, Any]] | None = None
            if scan_task is not None:
                self.session.message = "Waiting for LAN scan to finish…"
                try:
                    scan_results = await scan_task
                except Exception as e:  # noqa: BLE001
                    logger.warning("Bridge scan failed: %s", e)
                    self.session.warning = (
                        f"Bridge scan failed ({type(e).__name__}); used parent linking only."
                    )

            # Postprocess: "parent" links sub-devices to their gateway —
            # always needed so the bridge can route them. "all" additionally
            # bakes the bridge-scanned IP/version into each device. We pass
            # scan_results explicitly so tuyawizard skips its own
            # rustuya.Scanner fallback path (manager doesn't depend on the
            # rustuya Python crate anymore).
            mode = "all" if scan_results else "parent"
            self.session.message = f"Postprocessing ({mode})…"
            await loop.run_in_executor(None, postprocess_devices, devices, mode, scan_results)

            if self._on_devices is not None:
                await self._on_devices(devices)

            self.session.state = WizardState.DONE
            self.session.message = f"Done — {len(devices)} devices loaded"
        except asyncio.CancelledError:
            if scan_task is not None and not scan_task.done():
                scan_task.cancel()
            self.session.state = WizardState.ERROR
            self.session.error = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001 - any failure ends in ERROR with message
            if scan_task is not None and not scan_task.done():
                scan_task.cancel()
            logger.exception("Wizard flow failed")
            self.session.state = WizardState.ERROR
            self.session.error = f"{type(e).__name__}: {e}"
        finally:
            # Tear down tuyawizard's internals — closes the requests.Sessions
            # inside CustomerApi / LoginControl, stops the MQ thread if one
            # was spawned, calls Manager.unload() (HTTP terminal revoke),
            # and breaks the wizard ↔ manager ↔ token_listener reference
            # cycle. Without this, each TuyaWizard() leaked the underlying
            # urllib3 PoolManager + SSL context per wizard run — observed
            # ~750-950 KB/cycle on the Pi, identical magnitude to a stuck
            # paho-mqtt thread but actually requests-pool retention (which
            # was my first wrong guess). tuyawizard>=0.1.8 exposes close()
            # for exactly this; runs on signal / error / success paths.
            try:
                wizard.close()
            except Exception:  # noqa: BLE001
                logger.exception("tuyawizard.close() failed")


def _qr_to_data_url(text: str) -> str:
    """Encode `text` as a QR SVG and return it as a `data:image/svg+xml;base64,...`
    URL the browser can render with `<img src>`. SVG is sharper than PNG at any
    size and lets us avoid pulling Pillow as a dependency (qrcode's PNG backend
    needs PIL but doesn't declare it)."""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
