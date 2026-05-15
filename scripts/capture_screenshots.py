"""Generate documentation screenshots of the web UI.

Run manually after major UI changes; output PNGs live under
`docs/screenshots/` and are committed so the README references them
without an external CDN.

    python scripts/capture_screenshots.py

Boots uvicorn in a thread with a seeded State so the UI shows a
realistic mix of synced / missing / orphaned / mismatched devices plus
a gateway-with-sub-devices tree. Then drives Playwright through the
page at desktop + mobile viewports in light + dark themes.

Theme is driven via Playwright's `color_scheme` context option, which
makes `prefers-color-scheme: dark` return true; the page's inline
head script picks that up before first paint, so the screenshot is
never caught mid-toggle.

NOTE: Playwright's bundled chromium-headless-shell ships without a
color-emoji font, so the page's 🗑 / ☁ / 🌙 / ⟳ icons render as fallback
boxes. Install the system emoji font before running this script:

    sudo apt-get install -y fonts-noto-color-emoji   # Debian / Ubuntu

The CI Playwright job doesn't need this font — its tests assert against
the DOM, not the rendered glyphs — so this is a documentation-only
dependency.
"""

from __future__ import annotations

import random
import socket
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from rustuya_manager.models import Device
from rustuya_manager.state import BridgeTemplates, State
from rustuya_manager.web import build_app

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

# Seeded RNG keeps every regeneration of this script byte-identical, so a
# README screenshot refresh produces the same IDs / CIDs and the git diff
# stays focused on real visual changes.
_RNG = random.Random(42)


def _fake_id() -> str:
    """Tuya-shaped device id: `eb` prefix + 20 lowercase hex."""
    return "eb" + "".join(_RNG.choices("0123456789abcdef", k=20))


def _fake_cid() -> str:
    """Zigbee/BLE sub-node cid: 16 lowercase hex (no `eb` prefix)."""
    return "".join(_RNG.choices("0123456789abcdef", k=16))


class _StubBridgeClient:
    async def publish_command(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        pass


def _seed_state() -> State:
    """Produce a State that covers every sync category at least once.

    Distribution (intentional): 3 synced, 1 mismatched (RF Hub with a
    drifted IP), 2 missing (cloud-only), 1 orphan (bridge-only), plus a
    gateway + 2 sub-devices to exercise the tree renderer. Live status
    mixes online / offline so the dot colors don't all match.

    All identifiers are synthetic — never reuse real device IDs in
    documentation artifacts. Names are generic so brand strings don't
    leak through either. RFC1918 example subnet `192.168.1.x` keeps the
    LAN topology realistic without exposing any real network layout.
    """
    state = State()
    now = time.time()

    id_rf = _fake_id()
    id_plug = _fake_id()
    id_heavy_plug = _fake_id()
    id_zigbee_hub = _fake_id()
    id_wall_switch = _fake_id()
    id_door_sensor = _fake_id()
    id_ir_blaster = _fake_id()
    id_floor_lamp = _fake_id()
    id_orphan = _fake_id()

    cid_wall = _fake_cid()
    cid_door = _fake_cid()

    cloud = {
        id_rf: Device(
            id=id_rf, name="RF Hub", ip="192.168.1.10", version="3.3",
            key="aaaaaaaaaaaaaaaa",
        ),
        id_plug: Device(
            id=id_plug, name="Smart Plug", ip="192.168.1.11", version="3.3",
            key="bbbbbbbbbbbbbbbb",
        ),
        id_heavy_plug: Device(
            id=id_heavy_plug, name="Heavy Duty Outlet", ip="192.168.1.12",
            version="3.3", key="cccccccccccccccc",
        ),
        id_zigbee_hub: Device(
            id=id_zigbee_hub, name="Zigbee Hub", ip="192.168.1.20",
            version="3.3", key="dddddddddddddddd",
        ),
        id_wall_switch: Device(
            id=id_wall_switch, name="Wall Switch", type="SubDevice",
            cid=cid_wall, parent_id=id_zigbee_hub,
        ),
        id_door_sensor: Device(
            id=id_door_sensor, name="Door Sensor", type="SubDevice",
            cid=cid_door, parent_id=id_zigbee_hub,
        ),
        # Cloud-only (will render as "missing" — cards offer Add).
        id_ir_blaster: Device(
            id=id_ir_blaster, name="IR Blaster", ip="192.168.1.15",
            version="3.3", key="eeeeeeeeeeeeeeee",
        ),
        id_floor_lamp: Device(
            id=id_floor_lamp, name="Floor Lamp", ip="192.168.1.16",
            version="3.3", key="ffffffffffffffff",
        ),
    }
    state.cloud = cloud

    # Mismatched copy of the RF Hub — same id, drifted ip — gives the
    # diff engine something to flag without changing identity.
    bridge_rf = Device(
        id=id_rf, name="RF Hub", ip="192.168.1.99", version="3.3",
        key="aaaaaaaaaaaaaaaa",
    )
    state.bridge = {
        id_rf: bridge_rf,
        id_plug: cloud[id_plug],
        id_heavy_plug: cloud[id_heavy_plug],
        id_zigbee_hub: cloud[id_zigbee_hub],
        id_wall_switch: cloud[id_wall_switch],
        id_door_sensor: cloud[id_door_sensor],
        # Orphan — present in bridge, absent from cloud.
        id_orphan: Device(
            id=id_orphan, name="legacy-device", ip="192.168.1.50",
            version="3.3",
        ),
    }

    state.templates = BridgeTemplates(
        root="rustuya",
        command="rustuya/cmd/{action}",
        event="rustuya/event/{deviceid}/{dpid}",
        message="rustuya/{deviceid}/{action}",
        scanner="rustuya/scanner",
    )

    online = {"state": "online", "code": None, "message": None}
    offline = {"state": "offline", "code": None, "message": None}
    state.live_status = {
        id_rf: online,
        id_plug: online,
        id_heavy_plug: online,
        id_zigbee_hub: offline,
        id_wall_switch: online,
        id_door_sensor: online,
        id_orphan: online,
    }

    state.last_seen = {did: now - 30 - i * 15 for i, did in enumerate(state.bridge)}
    state.cloud_path = "/data/tuyadevices.json"
    return state


# Annotated overlay injected into the page right before the hero screenshot.
# Each callout sits in the page's right margin (only present when the
# viewport is wider than max-w-6xl = 1152px) with an SVG arrow drawn from
# the target element's right edge to the callout's left edge. Targets are
# located by device name text so the seed dict can change without breaking
# the annotation script.
_ANNOTATE_JS = r"""
() => {
    const style = document.createElement('style');
    style.textContent = `
        .demo-callout {
            position: absolute;
            background: #fef9c3;
            border: 2px solid #ca8a04;
            color: #422006;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 13px;
            font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
            font-weight: 500;
            line-height: 1.4;
            width: 200px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.18);
            z-index: 100;
        }
        .demo-callout b { color: #92400e; }
    `;
    document.head.appendChild(style);

    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    const docW = document.documentElement.scrollWidth;
    const docH = document.documentElement.scrollHeight;
    // Set both attribute (defines coordinate space) AND style (renders
    // at the right size). Without the width/height attributes, SVG
    // defaults to a 300×150 viewport and clips anything beyond.
    svg.setAttribute('width', docW);
    svg.setAttribute('height', docH);
    svg.style.position = 'absolute';
    svg.style.top = '0';
    svg.style.left = '0';
    svg.style.width = docW + 'px';
    svg.style.height = docH + 'px';
    svg.style.pointerEvents = 'none';
    svg.style.zIndex = '99';
    document.body.appendChild(svg);

    function findCardByName(name) {
        const spans = document.querySelectorAll('#device-list .font-medium');
        for (const s of spans) {
            if (s.textContent.trim() === name) {
                return s.closest('.rounded-lg');
            }
        }
        return null;
    }

    function annotate(target, html, calloutX, calloutY) {
        if (!target) return;
        const rect = target.getBoundingClientRect();
        const sX = window.scrollX, sY = window.scrollY;

        const callout = document.createElement('div');
        callout.className = 'demo-callout';
        callout.innerHTML = html;
        callout.style.left = calloutX + 'px';
        callout.style.top = (calloutY + sY) + 'px';
        document.body.appendChild(callout);

        const cRect = callout.getBoundingClientRect();
        const x1 = rect.right + sX;
        const y1 = rect.top + rect.height / 2 + sY;
        const x2 = cRect.left + sX;
        const y2 = cRect.top + cRect.height / 2 + sY;

        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', x1); line.setAttribute('y1', y1);
        // Line stops short of the callout so the arrowhead has room to sit.
        line.setAttribute('x2', x2 - 8); line.setAttribute('y2', y2);
        line.setAttribute('stroke', '#ca8a04');
        line.setAttribute('stroke-width', '3');
        line.setAttribute('stroke-linecap', 'round');
        svg.appendChild(line);

        // Filled triangle pointing right at the callout.
        const head = document.createElementNS(svgNS, 'polygon');
        head.setAttribute('points',
            (x2 - 9) + ',' + (y2 - 6) + ' ' +
            x2 + ',' + y2 + ' ' +
            (x2 - 9) + ',' + (y2 + 6));
        head.setAttribute('fill', '#ca8a04');
        svg.appendChild(head);

        // Origin dot at the target end so it's clear what's being pointed at.
        const dot = document.createElementNS(svgNS, 'circle');
        dot.setAttribute('cx', x1); dot.setAttribute('cy', y1);
        dot.setAttribute('r', '4'); dot.setAttribute('fill', '#ca8a04');
        svg.appendChild(dot);
    }

    // Callouts stack on a uniform 65px vertical rhythm (~50px tall +
    // ~15px gap) regardless of where their targets sit, so the right
    // column reads as an evenly-spaced legend. The arrows absorb any
    // small diagonal that introduces — readers track callout-to-target
    // by following the line, not by horizontal alignment.
    annotate(
        document.querySelector('header .ml-auto > div'),
        'Top-right: <b>+</b> add · <b>☁</b> cloud · <b>📡</b> scan · <b>🌙</b> theme · <b>⟳</b> refresh',
        1390, 15
    );
    annotate(
        document.querySelector('#sync-bar'),
        '<b>Bulk sync</b> — fix one category or apply all.',
        1390, 80
    );
    annotate(
        document.querySelector('#search-input').closest('.flex'),
        '<b>Search · filter · sort</b> — narrow the list.',
        1390, 145
    );
    annotate(
        findCardByName('legacy-device'),
        '<b>Orphan</b> — only in bridge. Click <b>🗑</b> to remove.',
        1390, 210
    );
    annotate(
        findCardByName('Floor Lamp'),
        '<b>Missing</b> — only in cloud. Click <b>Add</b> to publish.',
        1390, 275
    );
    annotate(
        findCardByName('RF Hub'),
        '<b>Mismatch</b> — click <b>Update</b> to push cloud → bridge.',
        1390, 340
    );
    annotate(
        findCardByName('Heavy Duty Outlet'),
        '<b>Synced</b> — in both, fields match. Click row to expand · live DPs.',
        1390, 420
    );
}
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(state: State, port: int) -> uvicorn.Server:
    app = build_app(state, _StubBridgeClient())
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("uvicorn did not start within 10s")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    state = _seed_state()
    port = _free_port()
    server = _start_server(state, port)
    url = f"http://127.0.0.1:{port}/"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            # Desktop, both themes. `color_scheme` makes the page's
            # `prefers-color-scheme` query return the chosen value, so
            # the inline <head> script applies the right `dark` class
            # before first paint.
            for theme in ("light", "dark"):
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900}, color_scheme=theme
                )
                page = ctx.new_page()
                page.goto(url)
                # Wait for any seeded device name to land — confirms WS
                # frame arrived and renderer ran.
                page.get_by_text("RF Hub").first.wait_for()
                page.screenshot(path=str(OUT / f"main-{theme}.png"))
                ctx.close()

            # Mobile (light) — narrow viewport exercises the hamburger,
            # the wrapped filter row, and the search/sort pairing.
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844}, color_scheme="light"
            )
            page = ctx.new_page()
            page.goto(url)
            page.get_by_text("RF Hub").first.wait_for()
            page.screenshot(path=str(OUT / "main-mobile.png"))
            ctx.close()

            # Sync modal opened — shows the bulk-sync flow.
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme="light"
            )
            page = ctx.new_page()
            page.goto(url)
            page.get_by_text("RF Hub").first.wait_for()
            page.locator("button[data-sync-scope='all']").click()
            page.locator("#sync-modal").wait_for(state="visible")
            page.screenshot(path=str(OUT / "sync-modal.png"))
            ctx.close()

            # Tuya Cloud wizard modal — open and capture the idle pane so
            # README readers see the user-code field plus the scan toggle.
            # The popover is left closed: it's absolute-positioned and
            # overflows the panel's bounding box, so an element-scoped
            # screenshot would clip it. README prose covers what the
            # toggle does instead.
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme="light"
            )
            page = ctx.new_page()
            page.goto(url)
            page.get_by_text("RF Hub").first.wait_for()
            page.locator("#wizard-header-btn").click()
            page.locator("#wizard-modal").wait_for(state="visible")
            # The first child of #wizard-modal is the rounded panel itself;
            # the outer wrapper is just a centered dimmed backdrop.
            page.locator("#wizard-modal > div").screenshot(
                path=str(OUT / "wizard-modal.png")
            )
            ctx.close()

            # Annotated hero — wider viewport so absolute-positioned
            # callouts live in the right margin and SVG arrows point at
            # the actual UI elements without overlapping card content.
            # All annotation HTML is injected post-render via
            # page.evaluate, so the underlying app code is untouched.
            #
            # `clip` drops the extra left margin that the wide viewport
            # introduces: content is centered (max-w-6xl + mx-auto) at
            # x=224 in a 1600-wide viewport. Clipping x=160..1600 brings
            # the content's left edge to x=64 in the saved image — the
            # same horizontal offset as the 1280-wide unannotated shots,
            # so the two screenshots feel like the same UI side-by-side.
            ctx = browser.new_context(
                viewport={"width": 1600, "height": 1000}, color_scheme="light"
            )
            page = ctx.new_page()
            page.goto(url)
            page.get_by_text("RF Hub").first.wait_for()
            page.evaluate(_ANNOTATE_JS)
            page.screenshot(
                path=str(OUT / "main-annotated.png"),
                clip={"x": 160, "y": 0, "width": 1440, "height": 900},
            )
            ctx.close()

            browser.close()
    finally:
        server.should_exit = True

    print(f"saved {len(list(OUT.glob('*.png')))} screenshots to {OUT}")


if __name__ == "__main__":
    main()
