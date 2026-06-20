"""Example rustuya-manager plugin — the reactive DP bus (api_version >= 2).

This package is intentionally NOT shipped with rustuya-manager. Install it into
the same environment to prove the reactive runtime end to end:

    pip install -e examples/derived_plugin
    # set RUSTUYA_DERIVED_DEVICE / _A / _B / _OUT to a real device + DPs

It demonstrates the now-task — *combine two Tuya DPs into a derived DP* — using
the single `register(ctx)` entry point and three reactive surfaces:

  - ctx.watch_device(device_id, handler)  — react to a device's events
  - ctx.derived_dp(device_id, dp).set()   — publish the combined value on the
                                            device's `{type}=derived` topic, for
                                            Home Assistant (or anything) to read
  - (ctx.set_device_dp is available too, for the external→Tuya direction)

The combiner here is a logical AND of two boolean DPs, but the point is the
shape: the plugin holds the latest values in a closure, recomputes on each
event, and publishes the result. Swap in any function of the inputs.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Called once by the manager's plugin host at app build time."""
    if ctx.api_version < 2:
        logger.warning(
            "derived plugin needs host api_version >= 2 (got %s) — skipping",
            ctx.api_version,
        )
        return

    device = os.environ.get("RUSTUYA_DERIVED_DEVICE", "demo-device")
    dp_a = os.environ.get("RUSTUYA_DERIVED_A", "1")
    dp_b = os.environ.get("RUSTUYA_DERIVED_B", "2")
    dp_out = os.environ.get("RUSTUYA_DERIVED_OUT", "99")

    out = ctx.derived_dp(device, dp_out)
    # Latest seen inputs, held across events in this closure.
    latest: dict[str, object] = {}

    async def on_event(device_id: str, dps: dict, origin: str) -> None:
        # Absorb whatever DPs this event carried, then recompute if we have both.
        latest.update(dps)
        if dp_a not in latest or dp_b not in latest:
            return
        combined = bool(latest[dp_a]) and bool(latest[dp_b])
        logger.debug(
            "derived %s/%s = %s AND %s = %s",
            device_id,
            dp_out,
            latest[dp_a],
            latest[dp_b],
            combined,
        )
        await out.set(combined)

    ctx.watch_device(device, on_event)
    logger.info("derived plugin: %s dp[%s]&dp[%s] -> derived dp[%s]", device, dp_a, dp_b, dp_out)
