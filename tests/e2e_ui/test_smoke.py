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
