#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Playwright coverage for the hosted reference server pages."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _operator_url(base_url: str, session_id: str = "demo-session") -> str:
    return f"{base_url}/app/operator/{session_id}"


def _user_url(base_url: str, session_id: str = "demo-session") -> str:
    return f"{base_url}/app/session/{session_id}"


class TestReferenceServerPages:
    def test_dashboard_links_to_operator_replay_and_quick_connect(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/", wait_until="domcontentloaded")

        expect(page.get_by_role("heading", name="provide-terminal-server")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Operator")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Replay")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Quick Connect")).to_be_visible(timeout=5000)
        expect(page.get_by_role("button", name="Refresh")).to_be_visible(timeout=5000)

    def test_quick_connect_page_renders_form_and_toggles_fields(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/connect", wait_until="domcontentloaded")

        expect(page.get_by_role("heading", name="Quick Connect")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="← Dashboard")).to_be_visible(timeout=5000)

        # SSH is the default: host/port and credentials visible
        expect(page.locator("#connect-host")).to_be_visible(timeout=5000)
        expect(page.locator("#connect-user")).to_be_visible(timeout=5000)

        # Switch to Telnet: host visible, SSH credentials hidden
        page.locator("#connect-type").select_option("telnet")
        expect(page.locator("#connect-host")).to_be_visible(timeout=2000)
        expect(page.locator("#connect-user")).to_be_hidden(timeout=2000)

        # Switch to Local Shell: host and credentials both hidden
        page.locator("#connect-type").select_option("shell")
        expect(page.locator("#connect-host")).to_be_hidden(timeout=2000)
        expect(page.locator("#connect-user")).to_be_hidden(timeout=2000)

        # Connect button present
        expect(page.get_by_role("button", name="Connect")).to_be_visible(timeout=2000)

    def test_quick_connect_shell_submits_and_redirects_to_session(self, page: Page, reference_server: str) -> None:
        page.goto(f"{reference_server}/app/connect", wait_until="domcontentloaded")

        page.locator("#connect-type").select_option("shell")
        page.locator("#connect-name").fill("E2E Shell Test")

        with page.expect_navigation(url=f"{reference_server}/app/session/**", timeout=8000):
            page.get_by_role("button", name="Connect").click()

        # Session page for the ephemeral shell session should be connected
        expect(page.get_by_role("heading", name="E2E Shell Test")).to_be_visible(timeout=5000)
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)

    def test_user_page_is_shared_and_not_operator_console(self, page: Page, reference_server: str) -> None:
        page.goto(_user_url(reference_server), wait_until="domcontentloaded")

        expect(page.get_by_role("heading", name="Interactive Shell Session")).to_be_visible(timeout=5000)
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)
        expect(page.locator("#btn-refresh")).to_have_count(0)
        expect(page.get_by_role("button", name="Hijack")).to_have_count(0)
        expect(page.locator("[id$='-inputfield']")).to_be_visible(timeout=5000)

    def test_operator_page_can_switch_modes_hijack_and_open_replay(self, page: Page, reference_server: str) -> None:
        page.goto(_operator_url(reference_server), wait_until="domcontentloaded")

        expect(page.get_by_role("button", name="Exclusive Mode")).to_be_visible(timeout=5000)
        page.get_by_role("button", name="Exclusive Mode").click()
        expect(page.get_by_role("button", name="Hijack")).to_be_visible(timeout=5000)
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (watching)", timeout=5000)

        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)

        page.get_by_role("button", name="Shared Mode").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)

        page.get_by_role("link", name="Replay").click()
        expect(page.get_by_role("heading", name="Interactive Shell Session (demo-session)")).to_be_visible(timeout=5000)
        expect(page.get_by_role("link", name="Download JSONL")).to_be_visible(timeout=5000)
        expect(page.locator("#replay-meta")).not_to_have_text("Loading recording…", timeout=5000)
        expect(page.locator("#replay-list button").first).to_be_visible(timeout=5000)
        expect(page.locator("#replay-scrubber")).to_be_enabled(timeout=5000)
        expect(page.locator("#replay-meta")).to_contain_text('"total":', timeout=5000)

        page.get_by_role("button", name="First").click()
        expect(page.locator("#replay-meta")).to_contain_text('"index": 1', timeout=5000)
        expect(page.locator("#replay-json")).to_contain_text('"event": "', timeout=5000)

        page.get_by_role("button", name="Next").click()
        expect(page.locator("#replay-meta")).to_contain_text('"index": 2', timeout=5000)

        page.locator("#replay-filter").select_option("read")
        expect(page.locator("#replay-meta")).to_contain_text('"filter": "read"', timeout=5000)
        expect(page.locator("#replay-json")).to_contain_text('"event": "read"', timeout=5000)

        page.locator("#replay-limit").select_option("25")
        expect(page.locator("#replay-meta")).to_contain_text('"limit": 25', timeout=5000)

        page.locator("#replay-scrubber").evaluate(
            "(el) => { el.value = String(el.max); el.dispatchEvent(new Event('input', { bubbles: true })); }"
        )
        scrubber_state = page.locator("#replay-scrubber").evaluate("(el) => ({ value: el.value, max: el.max })")
        assert scrubber_state["value"] == scrubber_state["max"]
