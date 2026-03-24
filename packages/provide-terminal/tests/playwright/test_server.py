#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright coverage for the hosted reference server pages."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _operator_url(base_url: str, session_id: str = "provide-shell") -> str:
    return f"{base_url}/app/operator/{session_id}"


def _user_url(base_url: str, session_id: str = "provide-shell") -> str:
    return f"{base_url}/app/session/{session_id}"


class TestReferenceServerPages:
    def test_dashboard_links_to_operator_replay_and_quick_connect(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/", wait_until="domcontentloaded")

        expect(page.get_by_text("Provide Terminal")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Operate").first).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Replay").first).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Quick connect")).to_be_visible(timeout=5000)
        expect(page.get_by_role("button", name="Refresh")).to_be_visible(timeout=5000)

    def test_quick_connect_page_renders_form_and_toggles_fields(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/connect", wait_until="domcontentloaded")

        expect(page.get_by_text("Quick connect")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Dashboard")).to_be_visible(timeout=5000)

        # Telnet is the default: host/port visible, no SSH credentials
        host_input = page.get_by_role("textbox", name="Host")
        expect(host_input).to_be_visible(timeout=5000)

        # Switch to SSH: host visible, SSH credentials visible
        page.get_by_role("combobox", name="Transport").select_option("ssh")
        expect(host_input).to_be_visible(timeout=2000)
        expect(page.get_by_role("textbox", name="Username")).to_be_visible(timeout=2000)

        # Switch to Provide Shell: host disabled (shows "(local)")
        page.get_by_role("combobox", name="Transport").select_option("shell")
        expect(page.get_by_role("textbox", name="Username")).to_be_hidden(timeout=2000)

        # Connect button present
        expect(page.get_by_role("button", name="Connect")).to_be_visible(timeout=2000)

    def test_quick_connect_shell_submits_and_redirects_to_session(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/connect", wait_until="domcontentloaded")

        page.get_by_role("combobox", name="Transport").select_option("shell")

        with page.expect_navigation(url=f"{reference_server}/app/session/**", timeout=8000):
            page.get_by_role("button", name="Connect").click()

        # Session page for the ephemeral shell session should be connected
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)

    def test_user_page_is_shared_and_not_operator_console(self, page: Page, reference_server: str) -> None:
        page.goto(_user_url(reference_server), wait_until="domcontentloaded")

        expect(page.get_by_text("Provide Shell").first).to_be_visible(timeout=5000)
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)
        expect(page.get_by_role("button", name="Hijack")).to_have_count(0)
        expect(page.locator("[id$='-inputfield']")).to_be_visible(timeout=5000)

    def test_operator_page_can_switch_modes_and_hijack(self, page: Page, reference_server: str) -> None:
        page.goto(_operator_url(reference_server), wait_until="domcontentloaded")

        expect(page.get_by_role("button", name="Exclusive")).to_be_visible(timeout=5000)
        page.get_by_role("button", name="Exclusive").click()
        expect(page.get_by_role("button", name="Hijack")).to_be_visible(timeout=5000)
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (watching)", timeout=5000)

        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)

        page.get_by_role("button", name="Shared").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)

    def test_replay_page_loads_events_and_navigation_works(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/replay/provide-shell", wait_until="domcontentloaded")

        # Breadcrumb shows session name and "Replay"
        expect(page.get_by_text("Replay")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Provide Shell")).to_be_visible(timeout=5000)

        # Events loaded (event count in header)
        expect(page.get_by_text("events").first).to_be_visible(timeout=5000)

        # Playback controls visible
        expect(page.get_by_role("button", name="▶")).to_be_visible(timeout=5000)

        # Navigate to first event
        page.get_by_role("button", name="|<").click()
        expect(page.get_by_text("Event 1 of")).to_be_visible(timeout=5000)

        # Event detail panel shows event type
        expect(page.get_by_text("Event detail")).to_be_visible(timeout=5000)

        # Navigate forward
        page.get_by_role("button", name=">").first.click()
        expect(page.get_by_text("Event 2 of")).to_be_visible(timeout=5000)

        # Screen preview shows snapshot caption
        expect(page.get_by_text("Screen snapshot at event")).to_be_visible(timeout=5000)
