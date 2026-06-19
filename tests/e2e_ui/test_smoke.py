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


def _apply_snapshot(page: Page, snap: dict, *, expanded: tuple[str, ...] = ()) -> None:
    """Inject a client-side WS snapshot and render it — resilient to the
    initial-load execution-context swap.

    A plain ``page.evaluate`` runs once and dies with "Execution context was
    destroyed, most likely because of a navigation" if Chromium swaps the
    document's execution context while the body runs — which intermittently
    races the ``goto``/load handshake right after page load. ``wait_for_function``
    re-installs and re-runs its body in the live context across such a swap, so
    the injection lands deterministically. The body is idempotent (set snapshot
    + render), so re-running is harmless.

    Caller must already have awaited a readiness signal (the ``conn-badge``
    showing "live") so the app's modules are loaded and the initial WS frame has
    arrived — otherwise a late initial frame could overwrite the injected snap.
    """
    page.wait_for_function(
        """(arg) => (async () => {
            const s = await import('/static/state.js');
            const r = await import('/static/render.js');
            for (const id of arg.expanded) s.expandedIds.add(id);
            s.state.snapshot = arg.snap;
            r.render();
            return true;
        })()""",
        arg={"snap": snap, "expanded": list(expanded)},
    )


def test_root_page_renders(page: Page, server_url: str) -> None:
    page.goto(server_url)
    expect(page.locator("h1")).to_have_text("rustuya-manager")


def test_theme_toggle_flips_html_dark_class(page: Page, server_url: str) -> None:
    page.goto(server_url)
    html = page.locator("html")
    initial_dark = "dark" in (html.get_attribute("class") or "")
    # All header actions live in the single #actions-menu now; open it first.
    page.locator("#actions-menu > summary").click()
    page.locator("#theme-btn").click()
    final_dark = "dark" in (html.get_attribute("class") or "")
    assert initial_dark != final_dark, "theme toggle did not flip the html class"


def test_language_switch_localizes_and_persists(page: Page, server_url: str) -> None:
    # The UI defaults to English (Chromium's locale is en-US). The hamburger
    # lists each locale as its own item (id `lang-<code>`) with the active one
    # checked; picking one re-localizes the static markup live, and the choice
    # survives a reload (persisted in localStorage).
    page.goto(server_url)
    all_tab = page.locator('#filter-tabs button[data-filter="all"] [data-i18n="filter.all"]')
    expect(all_tab).to_have_text("all")

    page.locator("#actions-menu > summary").click()
    # The picker is a collapsed submenu — one "Language" row by default; the
    # per-locale options are hidden until it's expanded.
    expect(page.locator("#lang-toggle")).to_be_visible()
    expect(page.locator("#lang-opt-ko")).to_have_count(0)
    # Expanding does NOT dismiss the dropdown (keepOpen), and reveals the list.
    page.locator("#lang-toggle").click()
    expect(page.locator("#lang-opt-en")).to_contain_text("✓")  # active
    expect(page.locator("#lang-opt-ko")).to_have_text("한국어")
    page.locator("#lang-opt-ko").click()
    # ko.json renders filter.all as "전체" — no reload needed (applyDom ran live).
    expect(all_tab).to_have_text("전체")
    expect(page.locator("html")).to_have_attribute("lang", "ko")

    # The choice persists: a reload re-boots the app and reads localStorage, and
    # the checkmark now sits on the Korean entry once the submenu is reopened.
    page.reload()
    expect(
        page.locator('#filter-tabs button[data-filter="all"] [data-i18n="filter.all"]')
    ).to_have_text("전체")
    page.locator("#actions-menu > summary").click()
    page.locator("#lang-toggle").click()
    expect(page.locator("#lang-opt-ko")).to_contain_text("✓")


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
    _apply_snapshot(page, snap, expanded=("dev-pwn",))
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
    assert key_cell.locator("script").count() == 0, "key value was injected as markup, not escaped"


def test_missing_card_renders_scan_row_with_diff_colors(page: Page, server_url: str) -> None:
    """When the bridge LAN scan has seen a missing-class device, the
    expanded card grows a SCAN IP / SCAN VER pair. Cells color-code:

      - amber when cloud is Auto/unset (informational)
      - rose when cloud has a value that disagrees with scan
      - plain when they match

    This is the only path that surfaces scan_results in the UI — Add
    still reads from cloud (api.js buildCommandBody), so the colored
    cells are display-only.
    """
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")

    snap = {
        "cloud": {
            # Cloud says Auto → amber SCAN IP, rose SCAN VER (cloud claims
            # 3.3 but the LAN device answered 3.4)
            "dev-auto-ip": {
                "id": "dev-auto-ip",
                "name": "auto-ip",
                "type": "WiFi",
                "key": "k" * 32,
                "ip": "Auto",
                "version": "3.3",
            },
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
        "diff": {"synced": [], "mismatched": [], "missing": ["dev-auto-ip"], "orphaned": []},
        "scan_results": {
            "dev-auto-ip": {
                "id": "dev-auto-ip",
                "ip": "192.168.1.42",
                "version": "3.4",
                "observed_at": 1700000000.0,
            },
        },
    }
    _apply_snapshot(page, snap, expanded=("dev-auto-ip",))

    # SCAN IP cell — cloud is "Auto" → amber color class
    scan_ip = page.locator("#device-list div").filter(has_text="SCAN IP").last
    expect(scan_ip).to_contain_text("192.168.1.42")
    expect(scan_ip.locator(".font-mono")).to_have_class(re.compile(r"text-amber-"))

    # SCAN VER cell — cloud has 3.3, scan saw 3.4 → rose color class
    scan_ver = page.locator("#device-list div").filter(has_text="SCAN VER").last
    expect(scan_ver).to_contain_text("3.4")
    expect(scan_ver.locator(".font-mono")).to_have_class(re.compile(r"text-rose-"))


def test_missing_card_omits_scan_row_when_no_sighting(page: Page, server_url: str) -> None:
    """Missing card with no scan_results entry for its id stays clean —
    we don't render an empty SCAN row, otherwise every cold-start UI
    would carry placeholder cells for nothing."""
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")

    snap = {
        "cloud": {
            "dev-no-scan": {
                "id": "dev-no-scan",
                "name": "no-scan",
                "type": "WiFi",
                "key": "k" * 32,
                "ip": "Auto",
                "version": "Auto",
            },
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
        "diff": {"synced": [], "mismatched": [], "missing": ["dev-no-scan"], "orphaned": []},
        "scan_results": {},
    }
    _apply_snapshot(page, snap, expanded=("dev-no-scan",))

    # No SCAN IP / SCAN VER rows when scan_results is empty
    assert page.locator("#device-list div").filter(has_text="SCAN IP").count() == 0
    assert page.locator("#device-list div").filter(has_text="SCAN VER").count() == 0


def test_collapsed_missing_card_scan_dot_reflects_visibility(page: Page, server_url: str) -> None:
    """Collapsed missing cards reuse the live-status dot slot to telegraph
    whether the LAN scan currently sees the device — filled sky when a
    sighting exists, dim ring otherwise. Both cards live in the same
    snapshot so we exercise the per-card branch in deviceCard, not just a
    global state."""
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")

    snap = {
        "cloud": {
            "dev-seen": {
                "id": "dev-seen",
                "name": "seen-by-scan",
                "type": "WiFi",
                "key": "k" * 32,
                "ip": "Auto",
                "version": "Auto",
            },
            "dev-unseen": {
                "id": "dev-unseen",
                "name": "not-seen-by-scan",
                "type": "WiFi",
                "key": "k" * 32,
                "ip": "Auto",
                "version": "Auto",
            },
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
        "diff": {
            "synced": [],
            "mismatched": [],
            "missing": ["dev-seen", "dev-unseen"],
            "orphaned": [],
        },
        "scan_results": {
            "dev-seen": {
                "id": "dev-seen",
                "ip": "192.168.1.42",
                "version": "3.4",
                "observed_at": 1700000000.0,
            },
        },
    }
    _apply_snapshot(page, snap)

    # Seen-by-scan card: filled sky dot exists, and the wrap's title
    # carries the observed IP so a hover explains the signal.
    seen_card = page.locator("#device-list > div").filter(has_text="seen-by-scan").first
    expect(seen_card.locator("span.bg-sky-500")).to_have_count(1)
    seen_title = seen_card.locator('span[title*="LAN scan"]').first.get_attribute("title")
    assert "192.168.1.42" in (seen_title or "")

    # Unseen card: no colored fill on any dot in this card, dim slate
    # ring, and the wrap title flags the ambiguity (scan-didn't-see vs
    # no-scan-yet) instead of pretending to know which.
    unseen_card = page.locator("#device-list > div").filter(has_text="not-seen-by-scan").first
    assert unseen_card.locator("span.bg-sky-500").count() == 0
    assert unseen_card.locator("span.bg-emerald-500").count() == 0
    expect(unseen_card.locator("span.border-slate-300")).to_have_count(1)
    unseen_title = unseen_card.locator('span[title*="LAN scan"]').first.get_attribute("title")
    assert "not in last LAN scan" in (unseen_title or "")


def test_drag_select_inside_expanded_card_does_not_collapse(page: Page, server_url: str) -> None:
    """An expanded card stays expanded when the user finishes a
    drag-to-select inside it — without this, dragging across IP/KEY/VER
    text would collapse the card on mouseup and the selection would
    vanish before Ctrl/Cmd-C could fire. Pin a snapshot, expand the
    card, programmatically set a text selection inside the KEY span,
    then synthesize a click on the card; the expanded state must
    survive."""
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")

    snap = {
        "cloud": {
            "dev-drag": {
                "id": "dev-drag",
                "name": "drag-target",
                "type": "WiFi",
                "key": "k" * 32,
                "ip": "192.168.1.42",
                "version": "3.4",
            },
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
        "diff": {"synced": [], "mismatched": [], "missing": ["dev-drag"], "orphaned": []},
        "scan_results": {},
    }
    _apply_snapshot(page, snap, expanded=("dev-drag",))

    card = page.locator("#device-list > div").first
    key_value = card.locator("div").filter(has_text="KEY").last.locator("span.font-mono")
    expect(key_value).to_be_visible()

    # Simulate the end state of a drag-to-select: a non-collapsed Range
    # anchored inside the KEY span. The click that follows mouseup must
    # not collapse the card.
    page.evaluate(
        """() => {
            const el = document.querySelector('#device-list > div span.font-mono');
            const range = document.createRange();
            range.selectNodeContents(el);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }"""
    )
    card.dispatch_event("click")

    # Card stayed expanded — KEY span is still visible.
    expect(key_value).to_be_visible()

    # Sanity: clearing the selection and clicking does still collapse
    # the card. Otherwise the test would pass even if the click handler
    # had been removed entirely.
    page.evaluate("() => window.getSelection().removeAllRanges()")
    card.dispatch_event("click")
    assert card.locator("div").filter(has_text="KEY").count() == 0


def test_scan_button_posts_to_api_scan(page: Page, server_url: str) -> None:
    """The header's 📡 Scan button drives the server-side
    LanScanCoordinator via POST /api/scan. Stub-app territory: the stub
    BridgeClient has no `subscribe_scanner`, so the request will fail at
    the server — we only assert the *client* sends the right request, the
    coordinator's behavior is exercised in tests/test_scan.py."""
    page.goto(server_url)
    # Scan lives in the single #actions-menu now; open it to reach the button.
    page.locator("#actions-menu > summary").click()
    button = page.locator("#scan-btn")
    expect(button).to_be_visible()

    with page.expect_request_finished(
        lambda req: req.url.endswith("/api/scan") and req.method == "POST"
    ):
        button.click()


def _bridge_info_snap(*, bridge_mode: str, embed_requested: bool, warnings=None) -> dict:
    """Minimal snapshot that renders the Bridge-info drawer fully (templates
    present so the drawer body, not just the badge, draws)."""
    return {
        "cloud": {},
        "bridge": {},
        "templates": {
            "root": "rustuya",
            "command": "rustuya/command",
            "event": "rustuya/event/{type}/{id}",
            "message": "rustuya/{level}/{id}",
            "scanner": "rustuya/scanner",
            "payload": "{value}",
        },
        "dps": {},
        "last_response": {},
        "last_seen": {},
        "retained_only": [],
        "live_status": {},
        "warnings": warnings or {},
        "cloud_loaded": True,
        "diff": {"synced": [], "mismatched": [], "missing": [], "orphaned": []},
        "device_count": 0,
        "mqtt_drop_count": 0,
        "bridge_version": "0.3.0",
        "bridge_mode": bridge_mode,
        "embed_requested": embed_requested,
    }


def test_bridge_info_drawer_label_and_external_badge(page: Page, server_url: str) -> None:
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")
    _apply_snapshot(page, _bridge_info_snap(bridge_mode="external", embed_requested=False))
    # Renamed drawer + a neutral "external" mode badge on the summary.
    summary = page.locator("#bridge-info-badge").locator("xpath=..")
    expect(summary).to_contain_text("Bridge info")
    badge = page.locator("#bridge-info-badge")
    expect(badge).to_be_visible()
    expect(badge).to_have_text("external")


def test_bridge_info_badge_flags_embed_external_conflict(page: Page, server_url: str) -> None:
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")
    _apply_snapshot(
        page,
        _bridge_info_snap(
            bridge_mode="external",
            embed_requested=True,
            warnings={
                "embedded_bridge_aborted": {
                    "level": "error",
                    "message": "--embed-bridge requested, but a bridge is already running on root 'rustuya'.",
                }
            },
        ),
    )
    badge = page.locator("#bridge-info-badge")
    expect(badge).to_contain_text("external")
    expect(badge).to_contain_text("⚠")
    # Amber emphasis on the conflict (visible while the drawer is collapsed).
    assert "amber" in (badge.get_attribute("class") or "")


def test_plugin_contributes_header_menu_item(page: Page, server_url_with_plugin: str) -> None:
    """A plugin's eager init.js (ctx.add_header_init → ctx.addHeaderAction) puts
    an item in the hamburger menu without the user opening the plugin's tab."""
    page.goto(server_url_with_plugin)
    expect(page.locator("#conn-badge")).to_contain_text("live")
    page.locator("#actions-menu > summary").click()
    action = page.locator("#e2e-plugin-action")
    expect(action).to_be_visible()
    expect(action).to_contain_text("Plugin action")
    action.click()
    # The init module's onClick stamps document.title as a side-effect probe.
    expect(page).to_have_title("plugin-action-fired")


def test_header_action_scoping_manager_only_vs_plugin_tab(
    page: Page, server_url_with_tab_plugin: str
) -> None:
    """Header-action scope is symmetric across manager and plugins:
    - "devices" built-ins (Reconfigure) show only on the manager's Devices view;
    - "global" built-ins (Restart) show on every tab;
    - a plugin action with the default scope shows only on that plugin's tab."""
    page.goto(server_url_with_tab_plugin)
    expect(page.locator("#conn-badge")).to_contain_text("live")

    # On the manager's Devices view.
    page.locator("#actions-menu > summary").click()
    expect(page.locator("#reconfigure-btn")).to_be_visible()  # manager-only
    expect(page.locator("#restart-btn")).to_be_visible()  # global
    expect(page.locator("#tabby-action")).to_be_hidden()  # plugin-tab-scoped
    # The language picker (collapsed submenu toggle) is global — the single
    # app-wide language control, visible on every tab.
    expect(page.locator("#lang-toggle")).to_be_visible()
    page.locator("#actions-menu > summary").click()  # close menu

    # Switch to the plugin's tab.
    page.locator('#page-tabs button[data-page="tabby"]').click()
    page.locator("#actions-menu > summary").click()
    expect(page.locator("#tabby-action")).to_be_visible()  # now on its own tab
    expect(page.locator("#restart-btn")).to_be_visible()  # global everywhere
    expect(page.locator("#reconfigure-btn")).to_be_hidden()  # manager-only, hidden here
    # The language picker is global, so it stays visible on plugin tabs too — one
    # switcher for the whole UI; plugins follow the shell's language.
    expect(page.locator("#lang-toggle")).to_be_visible()


def test_plugin_reload_menu_items_present(page: Page, server_url: str) -> None:
    """The hamburger menu exposes both reload paths: add-only "Load new plugins"
    and the full-reload "Restart manager". (We don't click Restart — it would
    re-exec the test server; the endpoint itself is covered in test_web.py.)"""
    page.goto(server_url)
    expect(page.locator("#conn-badge")).to_contain_text("live")
    page.locator("#actions-menu > summary").click()
    scan = page.locator("#plugin-scan-btn")
    expect(scan).to_be_visible()
    expect(scan).to_contain_text("Load new plugins")
    restart = page.locator("#restart-btn")
    expect(restart).to_be_visible()
    expect(restart).to_contain_text("Restart manager")
    # "Load new plugins" is safe to fire on the stub server (no plugin dir →
    # added 0); it must not throw and the menu closes.
    scan.click()
    expect(page.locator("#toast-container")).to_contain_text("No new plugins found")
