"""Playwright smoke tests for the web UI.

Scope is deliberately small — page renders, a handful of interactions
work — so flakiness has nowhere to hide on day one. Every assertion
uses Playwright's `expect()` so the framework auto-retries until
condition or timeout, which means no manual sleeps and no race
conditions on initial load.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def test_root_page_renders(page: Page, server_url: str) -> None:
    page.goto(server_url)
    expect(page.locator("h1")).to_have_text("rustuya-manager")


def test_theme_toggle_flips_html_dark_class(page: Page, server_url: str) -> None:
    page.goto(server_url)
    html = page.locator("html")
    initial_dark = "dark" in (html.get_attribute("class") or "")
    # First-match handles both the desktop button and the mobile menu
    # twin; whichever is visible at the viewport's media query wins.
    page.locator("#theme-btn, [data-mobile-action='theme-btn']").first.click()
    final_dark = "dark" in (html.get_attribute("class") or "")
    assert initial_dark != final_dark, "theme toggle did not flip the html class"


def test_search_clear_button_visibility_tracks_input(page: Page, server_url: str) -> None:
    page.goto(server_url)
    search = page.locator("#search-input")
    clear = page.locator("#search-clear")

    # Empty input → clear button hidden
    expect(clear).to_be_hidden()

    search.fill("hello")
    expect(clear).to_be_visible()

    clear.click()
    expect(search).to_have_value("")
    expect(clear).to_be_hidden()


def test_filter_all_pill_is_active_by_default(page: Page, server_url: str) -> None:
    page.goto(server_url)
    all_pill = page.locator("button[data-filter='all']")
    # Active styling uses bg-slate-700 (light theme) or bg-slate-200
    # (dark theme); `to_have_class` with a regex auto-retries until the
    # WebSocket-driven initial render lands one of the two, so this
    # doesn't race the first frame.
    expect(all_pill).to_have_class(re.compile(r"bg-slate-(700|200)"))


def test_wizard_modal_opens_and_closes_on_escape(page: Page, server_url: str) -> None:
    page.goto(server_url)
    # No cloud is loaded in this stub, so the cloud-banner's "Connect
    # Tuya Cloud" CTA is the entry point. The wizard modal should appear
    # and dismiss on ESC — the regression that was fixed in commit
    # 1e8d351.
    page.locator("#wizard-open-btn").click()
    modal = page.locator("#wizard-modal")
    expect(modal).to_be_visible()
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()


def test_expanded_card_shows_full_key_and_escapes_special_chars(
    page: Page, server_url: str
) -> None:
    """KEY is shown in full (no shortening) and any HTML metacharacters in
    the key value land as text, not interpreted markup.

    The dom helpers' `escapeHtml` should HTML-escape `<`, `>`, `&`, `"` before
    they go into innerHTML. We assert two things:
      1. the rendered KEY span's textContent equals the raw key (no
         truncation, no entity-mangling)
      2. no <script> child was injected by the renderer (would mean
         the key bypassed escaping)
    """
    # If escaping ever regresses, a literal <script>alert(...)</script> would
    # try to fire — pre-register a dialog dismisser so the test reports the
    # escape failure cleanly rather than hanging on a modal.
    page.on("dialog", lambda d: d.dismiss())
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server_url)
    # Wait for the initial WS frame so /static/state.js has already been
    # imported by app.js — our subsequent `import('/static/state.js')`
    # then gets the same module instance (singleton state object).
    expect(page.locator("#conn-badge")).to_contain_text("live")

    raw_key = '<script>alert("xss")</script>&"x'
    snap = {
        "cloud": {
            "dev-pwn": {
                "id": "dev-pwn",
                "name": "test-device",
                "type": "WiFi",
                "key": raw_key,
                "ip": "Auto",
                "version": "3.4",
            }
        },
        "bridge": {},
        "templates": None,
        "dps": {},
        "last_response": {},
        "last_seen": {},
        "retained_only": [],
        "live_status": {},
        "warnings": {},
        "cloud_loaded": True,
        "diff": {"synced": [], "mismatched": [], "missing": ["dev-pwn"], "orphaned": []},
    }
    page.evaluate(
        """async (snap) => {
            const s = await import('/static/state.js');
            const r = await import('/static/render.js');
            s.expandedIds.add('dev-pwn');
            s.state.snapshot = snap;
            r.render();
        }""",
        snap,
    )
    assert not errors, f"page errors during render: {errors}"

    # Missing-class card should have the sky edge stripe (cards.js
    # computeEdgeColor). This also exercises the missing → expand path —
    # the same expand UI as paired devices, surfacing IP/KEY/VER.
    card = page.locator("#device-list > div").first
    expect(card).to_have_class(re.compile(r"border-l-sky-"))

    # The grid renders KEY in a labeled cell. Locate the value span via its
    # label sibling — keeps the test resilient to layout class churn.
    key_cell = page.locator("#device-list div").filter(has_text="KEY").last
    # The full 32-char-style key should appear verbatim as text, NOT as
    # an interpreted <script> element.
    expect(key_cell).to_contain_text(raw_key)
    # If escaping had failed, a <script> child would exist inside the cell.
    assert key_cell.locator("script").count() == 0, (
        "key value was injected as markup, not escaped"
    )


def test_scan_button_posts_to_api_scan(page: Page, server_url: str) -> None:
    """The header's 📡 Scan button drives the server-side
    LanScanCoordinator via POST /api/scan. Stub-app territory: the stub
    BridgeClient has no `subscribe_scanner`, so the request will fail at
    the server — we only assert the *client* sends the right request, the
    coordinator's behavior is exercised in tests/test_scan.py."""
    page.goto(server_url)
    button = page.locator("#scan-btn")
    expect(button).to_be_visible()

    with page.expect_request_finished(
        lambda req: req.url.endswith("/api/scan") and req.method == "POST"
    ):
        button.click()
