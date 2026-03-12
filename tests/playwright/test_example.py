#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Playwright coverage for the real interactive example page."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def _example_url(base_url: str) -> str:
    return f"{base_url}/hijack/hijack.html?worker=demo-session"


def _example_reset(base_url: str, mode: str = "hijack") -> None:
    with httpx.Client(base_url=base_url, timeout=5.0) as http:
        reset = http.post("/demo/session/demo-session/reset")
        assert reset.status_code == 200
        switch = http.post("/demo/session/demo-session/mode", json={"input_mode": mode})
        assert switch.status_code == 200


def _example_state(base_url: str) -> dict[str, object]:
    with httpx.Client(base_url=base_url, timeout=5.0) as http:
        resp = http.get("/demo/session/demo-session")
        resp.raise_for_status()
        return resp.json()


def _navigate_example(page: Page, base_url: str) -> None:
    page.goto(_example_url(base_url), wait_until="domcontentloaded")


class TestExamplePageSingleBrowser:
    def test_hijacked_input_updates_example_state_and_analysis(self, page: Page, example_server: str) -> None:
        _example_reset(example_server, mode="hijack")

        _navigate_example(page, example_server)
        expect(page.locator("#demo-session-status")).to_contain_text("demo-session", timeout=5000)
        expect(page.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)

        page.locator("[id$='-inputfield']").fill("hello from playwright")
        page.get_by_role("button", name="Send").click()

        state = _example_state(example_server)
        transcript = state.get("transcript", [])
        assert isinstance(transcript, list)
        from typing import cast

        assert any(
            "hello from playwright" in str(cast("dict", entry).get("text", ""))
            for entry in transcript
            if isinstance(entry, dict)
        )

        page.get_by_role("button", name="Analyze").click()
        expect(page.locator("[id$='-analysistext']")).to_contain_text("interactive demo analysis", timeout=5000)

        page.locator("#demo-reset").click()
        expect(page.locator("#demo-session-note")).to_contain_text("Session reset.", timeout=5000)
        state_after = _example_state(example_server)
        transcript_after = state_after["transcript"]
        assert isinstance(transcript_after, list)
        assert len(transcript_after) == 2

    def test_command_flow_from_page_controls(self, page: Page, example_server: str) -> None:
        _example_reset(example_server, mode="hijack")

        _navigate_example(page, example_server)
        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)

        for command in ("/help", "/mode open", "/mode hijack"):
            page.locator("[id$='-inputfield']").fill(command)
            page.get_by_role("button", name="Send").click()

        expect(page.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)
        page.locator("[id$='-inputfield']").fill("/clear")
        page.get_by_role("button", name="Send").click()

        state = _example_state(example_server)
        assert state["input_mode"] == "hijack"
        assert state["pending_banner"] == "Transcript cleared."


class TestExamplePageTwoBrowsers:
    def test_two_browser_handoff_in_exclusive_mode(self, page: Page, browser: object, example_server: str) -> None:
        _example_reset(example_server, mode="hijack")

        _navigate_example(page, example_server)
        expect(page.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)

        ctx2 = browser.new_context()  # type: ignore[attr-defined]
        page2 = ctx2.new_page()
        try:
            _navigate_example(page2, example_server)
            expect(page2.locator("[id$='-statustext']")).to_have_text("Hijacked (other)", timeout=5000)
            expect(page2.get_by_role("button", name="Hijack")).to_be_disabled(timeout=5000)

            page.get_by_role("button", name="Release").click()
            expect(page2.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
            page2.get_by_role("button", name="Hijack").click()

            expect(page2.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)
            expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (other)", timeout=5000)
        finally:
            page2.close()
            ctx2.close()

    def test_two_browsers_can_type_in_shared_mode(self, page: Page, browser: object, example_server: str) -> None:
        _example_reset(example_server, mode="hijack")

        _navigate_example(page, example_server)
        page.select_option("#demo-mode", "open")
        page.locator("#demo-apply").click()
        expect(page.locator("#demo-session-status")).to_contain_text("open", timeout=5000)

        ctx2 = browser.new_context()  # type: ignore[attr-defined]
        page2 = ctx2.new_page()
        try:
            _navigate_example(page2, example_server)
            expect(page2.locator("#demo-session-status")).to_contain_text("open", timeout=5000)
            expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)
            expect(page2.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)

            page.locator("[id$='-inputfield']").fill("from first browser")
            page.get_by_role("button", name="Send").click()
            page2.locator("[id$='-inputfield']").fill("from second browser")
            page2.get_by_role("button", name="Send").click()

            state = _example_state(example_server)
            transcript = state.get("transcript", [])
            assert isinstance(transcript, list)
            from typing import cast

            assert any(
                "from first browser" in str(cast("dict", entry).get("text", ""))
                for entry in transcript
                if isinstance(entry, dict)
            )
            assert any(
                "from second browser" in str(cast("dict", entry).get("text", ""))
                for entry in transcript
                if isinstance(entry, dict)
            )
        finally:
            page2.close()
            ctx2.close()
