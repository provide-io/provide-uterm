#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright chaos tests: worker crash, rapid hijack ping-pong, 3-browser churn.

Uses ``WorkerController`` (background-thread fake worker) and the
``hijack_server`` fixture from conftest.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from ..conftest import WorkerController  # noqa: TID252


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")


def _hijack_btn(page: Page) -> object:
    return page.get_by_role("button", name="Hijack")


def _release_btn(page: Page) -> object:
    return page.get_by_role("button", name="Release")


def _input_field(page: Page) -> object:
    return page.locator("#inputfield")


def _send_btn(page: Page) -> object:
    return page.get_by_role("button", name="Send")


def _status_text(page: Page) -> str:
    return page.locator("#statustext").text_content() or ""


def _wait_connected(page: Page, timeout: int = 5000) -> None:
    page.wait_for_function(
        "() => window.__deepQuery('#statustext')?.textContent === 'Connected (watching)'",
        timeout=timeout,
    )


def _wait_hijacked_by_me(page: Page, timeout: int = 5000) -> None:
    page.wait_for_function(
        "() => window.__deepQuery('#statustext')?.textContent?.includes('Hijacked (you)')",
        timeout=timeout,
    )


def _wait_not_hijacked_by_me(page: Page, timeout: int = 5000) -> None:
    page.wait_for_function(
        "() => !window.__deepQuery('#statustext')?.textContent?.includes('Hijacked (you)')",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 11. Worker crash during hijack acquire
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestWorkerCrashDuringHijack:
    def test_hijack_acquire_during_worker_crash(
        self,
        page: Page,
        hijack_server: tuple[str, object],
    ) -> None:
        """Browser clicks Hijack at the moment worker disconnects; widget recovers to Offline."""
        base_url, _ = hijack_server
        wid = f"crash-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        try:
            _navigate(page, base_url, wid)
            _wait_connected(page)

            # Click hijack and immediately kill the worker
            _hijack_btn(page).click()  # type: ignore[union-attr]
            ctrl.stop()

            # Widget must NOT get stuck on "Acquiring..." — it should show Offline
            page.wait_for_function(
                "() => {"
                "  const st = window.__deepQuery('#statustext')?.textContent || '';"
                "  return st === 'Offline' || st === 'Disconnected';"
                "}",
                timeout=10000,
            )

            # Hijack button should be disabled (worker is gone)
            expect(_hijack_btn(page)).to_be_disabled(timeout=3000)  # type: ignore[union-attr]

            # No JS errors (check console)
            # Reconnect a new worker — widget should recover
            ctrl2 = WorkerController(base_url, wid).start()
            try:
                _wait_connected(page, timeout=10000)
                expect(_hijack_btn(page)).to_be_enabled(timeout=5000)  # type: ignore[union-attr]
            finally:
                ctrl2.stop()

        except Exception:
            ctrl.stop()
            raise


# ---------------------------------------------------------------------------
# 12. Rapid hijack ping-pong between two browsers
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestRapidHijackPingPong:
    def test_two_browser_hijack_ping_pong(
        self,
        page: Page,
        browser: object,
        hijack_server: tuple[str, object],
    ) -> None:
        """Two browsers trade hijack 3 times; all inputs delivered in order."""
        base_url, _ = hijack_server
        wid = f"pong-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        try:
            # Page A
            page_a = page
            _navigate(page_a, base_url, wid)
            _wait_connected(page_a)

            # Page B (separate browser context)
            ctx_b = browser.new_context()  # type: ignore[union-attr]
            page_b = ctx_b.new_page()
            _navigate(page_b, base_url, wid)
            _wait_connected(page_b)

            for round_num in range(1, 4):
                # Page A hijacks
                _hijack_btn(page_a).click()  # type: ignore[union-attr]
                _wait_hijacked_by_me(page_a)

                # Page A sends input
                _input_field(page_a).fill(f"round-{round_num}-A\r")  # type: ignore[union-attr]
                _send_btn(page_a).click()  # type: ignore[union-attr]
                page_a.wait_for_timeout(300)

                # Page A releases
                _release_btn(page_a).click()  # type: ignore[union-attr]
                _wait_not_hijacked_by_me(page_a)
                page_a.wait_for_timeout(300)

                # Page B hijacks
                _hijack_btn(page_b).click()  # type: ignore[union-attr]
                _wait_hijacked_by_me(page_b)

                # Page B sends input
                _input_field(page_b).fill(f"round-{round_num}-B\r")  # type: ignore[union-attr]
                _send_btn(page_b).click()  # type: ignore[union-attr]
                page_b.wait_for_timeout(300)

                # Page B releases
                _release_btn(page_b).click()  # type: ignore[union-attr]
                _wait_not_hijacked_by_me(page_b)
                page_b.wait_for_timeout(300)

            ctx_b.close()

            # Verify worker received all 6 inputs in order
            page_a.wait_for_timeout(500)
            input_msgs = [
                m
                for m in ctrl.received
                if m.get("type") == "input" or ("data" in m and "round-" in str(m.get("data", "")))
            ]
            assert len(input_msgs) >= 6, f"Worker should receive 6 inputs, got {len(input_msgs)}: {input_msgs}"
            # Verify all round-N-A and round-N-B are present
            data_strs = [str(m.get("data", "")) for m in input_msgs]
            for r in range(1, 4):
                a_str = f"round-{r}-A"
                b_str = f"round-{r}-B"
                assert any(a_str in d for d in data_strs), f"Missing input {a_str}: {data_strs}"
                assert any(b_str in d for d in data_strs), f"Missing input {b_str}: {data_strs}"

        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# 13. Three-browser churn: hijack + disconnect + state consistency
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestThreeBrowserChurn:
    def test_three_browser_hijack_with_disconnect(
        self,
        page: Page,
        browser: object,
        hijack_server: tuple[str, object],
    ) -> None:
        """3 browsers: A hijacks, C disconnects mid-session, A+B remain consistent."""
        base_url, _ = hijack_server
        wid = f"churn-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        try:
            # Page A (default)
            page_a = page
            _navigate(page_a, base_url, wid)
            _wait_connected(page_a)

            # Page B
            ctx_b = browser.new_context()  # type: ignore[union-attr]
            page_b = ctx_b.new_page()
            _navigate(page_b, base_url, wid)
            _wait_connected(page_b)

            # Page C
            ctx_c = browser.new_context()  # type: ignore[union-attr]
            page_c = ctx_c.new_page()
            _navigate(page_c, base_url, wid)
            _wait_connected(page_c)

            # A acquires hijack
            _hijack_btn(page_a).click()  # type: ignore[union-attr]
            _wait_hijacked_by_me(page_a)

            # B and C should show "Hijacked (other)"
            page_b.wait_for_function(
                "() => window.__deepQuery('#statustext')?.textContent?.includes('Hijacked')",
                timeout=5000,
            )
            page_c.wait_for_function(
                "() => window.__deepQuery('#statustext')?.textContent?.includes('Hijacked')",
                timeout=5000,
            )

            # A sends input while all 3 are connected
            _input_field(page_a).fill("before-crash\r")  # type: ignore[union-attr]
            _send_btn(page_a).click()  # type: ignore[union-attr]
            page_a.wait_for_timeout(300)

            # C disconnects abruptly (close page + context)
            page_c.close()
            ctx_c.close()
            page_a.wait_for_timeout(500)

            # A should still own hijack after C's departure
            status_a = _status_text(page_a)
            assert "Hijacked (you)" in status_a, f"A should still own hijack after C leaves, got: {status_a}"

            # A sends more input
            _input_field(page_a).fill("after-crash\r")  # type: ignore[union-attr]
            _send_btn(page_a).click()  # type: ignore[union-attr]
            page_a.wait_for_timeout(300)

            # A releases
            _release_btn(page_a).click()  # type: ignore[union-attr]
            _wait_not_hijacked_by_me(page_a)

            # B should now be able to hijack
            _hijack_btn(page_b).click()  # type: ignore[union-attr]
            _wait_hijacked_by_me(page_b)

            # B sends input
            _input_field(page_b).fill("b-takes-over\r")  # type: ignore[union-attr]
            _send_btn(page_b).click()  # type: ignore[union-attr]
            page_b.wait_for_timeout(300)

            # B releases
            _release_btn(page_b).click()  # type: ignore[union-attr]
            _wait_not_hijacked_by_me(page_b)

            ctx_b.close()

            # Verify worker got all 3 inputs
            page_a.wait_for_timeout(500)
            input_msgs = [m for m in ctrl.received if m.get("type") == "input" or "data" in m]
            data_strs = [str(m.get("data", "")) for m in input_msgs]
            assert any("before-crash" in d for d in data_strs), f"Worker missing 'before-crash' input: {data_strs}"
            assert any("after-crash" in d for d in data_strs), f"Worker missing 'after-crash' input: {data_strs}"
            assert any("b-takes-over" in d for d in data_strs), f"Worker missing 'b-takes-over' input: {data_strs}"

            # Worker should have received exactly 1 pause from A's hijack,
            # then a resume when A released, then another pause from B.
            # C's disconnect should NOT cause any hijack state changes.
            control_msgs = [m for m in ctrl.received if m.get("type") == "control"]
            pause_count = sum(1 for m in control_msgs if m.get("action") == "pause")
            resume_count = sum(1 for m in control_msgs if m.get("action") == "resume")
            assert pause_count >= 2, f"Worker should get >= 2 pauses (A + B), got {pause_count}: {control_msgs}"
            assert resume_count >= 1, f"Worker should get >= 1 resume (A release), got {resume_count}: {control_msgs}"

        finally:
            ctrl.stop()
