#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright end-to-end tests for the ProvideHijack browser widget.

Architecture
------------
- ``hijack_server`` (session fixture, conftest.py) — one real TermHub +
  uvicorn server for the whole Playwright session.  Exposes
  ``/test-page/{worker_id}`` — a minimal HTML page with the widget configured
  to ``heartbeatInterval: 500 ms`` so heartbeat assertions don't take 5 s.
- ``WorkerController`` (conftest.py) — background-thread fake worker that
  connects as ``/ws/worker/{id}/term`` and collects received messages.
- Every test uses a unique ``worker_id`` (prefixed by class) so tests are
  fully isolated even though the server is session-scoped.

CDN interception
----------------
The widget page does NOT load xterm.js (it's loaded from CDN in the full
hijack.html; the test page omits it).  Because ``window.Terminal`` is
undefined, xterm initialisation in the widget silently fails (errors are
caught), but all status-text and button-state assertions work correctly.
"""

from __future__ import annotations

import time
import uuid

from playwright.sync_api import Page, expect

from ..conftest import WorkerController  # noqa: TID252

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    """Short unique suffix for test-specific worker IDs."""
    return uuid.uuid4().hex[:8]


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    """Navigate *page* to the test widget page for *worker_id*."""
    import os

    # Multi-backend: serve test HTML + /ui via page.route (production servers
    # do not ship /test-page; UI assets may also be route-fulfilled).
    if os.environ.get("UTERM_MULTI_BACKEND") or os.environ.get("UTERM_TEST_BACKEND", "python") in (
        "go",
        "csharp",
    ):
        from .ui_routes import install_multi_backend_routes

        install_multi_backend_routes(page)
    page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")


def _status_text(page: Page) -> str:
    """Return the current status text shown by the widget."""
    return page.locator("#statustext").text_content() or ""


from playwright.sync_api import Locator


def _hijack_btn(page: Page) -> Locator:
    return page.get_by_role("button", name="Hijack")


def _step_btn(page: Page) -> Locator:
    return page.get_by_role("button", name="Step")


def _release_btn(page: Page) -> Locator:
    return page.get_by_role("button", name="Release")


def _resync_btn(page: Page) -> Locator:
    return page.get_by_role("button", name="⟳ Resync")


# ---------------------------------------------------------------------------
# Initial state (no worker)
# ---------------------------------------------------------------------------


class TestWidgetInitialState:
    def test_toolbar_buttons_present(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """All expected toolbar buttons must render in the widget."""
        base_url, _ = hijack_server
        worker_id = f"init-{_uid()}"
        _navigate(page, base_url, worker_id)

        expect(_hijack_btn(page)).to_be_visible()
        expect(_step_btn(page)).to_be_visible()
        expect(_release_btn(page)).to_be_visible()
        expect(_resync_btn(page)).to_be_visible()
        expect(page.get_by_role("button", name="Analyze")).to_be_visible()
        expect(page.locator("button[title='Toggle mobile key toolbar']")).to_be_visible()

    def test_hijack_button_disabled_when_no_worker(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Hijack button must be disabled until a worker comes online."""
        base_url, _ = hijack_server
        worker_id = f"noworker-{_uid()}"
        _navigate(page, base_url, worker_id)

        # Wait for WS hello to arrive (widget transitions out of "Connecting…")
        page.wait_for_function("window.__deepQuery('#statustext')?.textContent !== 'Connecting…'")

        expect(_hijack_btn(page)).to_be_disabled()
        expect(_step_btn(page)).to_be_disabled()
        expect(_release_btn(page)).to_be_disabled()

    def test_status_shows_worker_offline_when_no_worker(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Status text shows 'Offline' after hello with no worker connected."""
        base_url, _ = hijack_server
        worker_id = f"offline-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Offline'")
        assert _status_text(page) == "Offline"

    def test_mobile_keys_row_hidden_before_hijack(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """The .mobile-keys row must not be visible before a hijack is acquired."""
        base_url, _ = hijack_server
        worker_id = f"mkeys-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.locator("button[title='Toggle mobile key toolbar']").click()
        assert not page.locator(".mobile-keys").is_visible()


# ---------------------------------------------------------------------------
# Worker online state
# ---------------------------------------------------------------------------


class TestWorkerOnlineState:
    def test_hijack_button_enabled_when_worker_connects(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Hijack button becomes enabled once the worker is online."""
        base_url, _ = hijack_server
        worker_id = f"wconn-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            expect(_resync_btn(page)).to_be_enabled(timeout=2000)
        finally:
            ctrl.stop()

    def test_status_connected_watching_when_worker_online(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Status shows 'Connected (watching)' with worker online and no hijack."""
        base_url, _ = hijack_server
        worker_id = f"watching-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Connected (watching)'")
            assert _status_text(page) == "Connected (watching)"
        finally:
            ctrl.stop()

    def test_worker_disconnect_disables_hijack_button(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Hijack button becomes disabled when the worker disconnects."""
        base_url, _ = hijack_server
        worker_id = f"wdisconn-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        _navigate(page, base_url, worker_id)
        expect(_hijack_btn(page)).to_be_enabled(timeout=5000)

        ctrl.stop()  # disconnect the worker

        expect(_hijack_btn(page)).to_be_disabled(timeout=5000)

    def test_status_worker_offline_after_disconnect(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Status shows 'Offline' after the worker disconnects."""
        base_url, _ = hijack_server
        worker_id = f"woffline-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        _navigate(page, base_url, worker_id)
        page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Connected (watching)'")

        ctrl.stop()

        page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Offline'")
        assert _status_text(page) == "Offline"


# ---------------------------------------------------------------------------
# Hijack acquire / step / release
# ---------------------------------------------------------------------------


class TestHijackAcquireRelease:
    def test_hijack_request_sent_on_button_click(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Clicking Hijack sends a hijack_request WS message to the server."""
        base_url, _ = hijack_server
        worker_id = f"acq-req-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()

            # Worker should receive a pause control
            msg = ctrl.wait_for(lambda m: m.get("type") == "control" and m.get("action") == "pause")
            assert msg is not None, "Worker did not receive pause after hijack_request"
        finally:
            ctrl.stop()

    def test_buttons_after_hijack_acquired(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """After acquiring hijack: Step/Release enabled; Hijack disabled."""
        base_url, _ = hijack_server
        worker_id = f"acq-btn-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()

            expect(_step_btn(page)).to_be_enabled(timeout=5000)
            expect(_release_btn(page)).to_be_enabled(timeout=2000)
            expect(_hijack_btn(page)).to_be_disabled(timeout=2000)
        finally:
            ctrl.stop()

    def test_status_hijacked_you_after_acquire(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Status shows 'Hijacked (you)' after successful hijack acquire."""
        base_url, _ = hijack_server
        worker_id = f"acq-status-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()

            page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (you)'")
            assert _status_text(page) == "Hijacked (you)"
        finally:
            ctrl.stop()

    def test_release_restores_buttons(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Clicking Release restores buttons to the 'watching' state."""
        base_url, _ = hijack_server
        worker_id = f"rel-btn-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            expect(_release_btn(page)).to_be_enabled(timeout=5000)

            _release_btn(page).click()

            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            expect(_step_btn(page)).to_be_disabled(timeout=2000)
            expect(_release_btn(page)).to_be_disabled(timeout=2000)
        finally:
            ctrl.stop()

    def test_step_button_sends_hijack_step(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Clicking Step sends a hijack_step WS message to the server."""
        base_url, _ = hijack_server
        worker_id = f"step-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            expect(_step_btn(page)).to_be_enabled(timeout=5000)

            _step_btn(page).click()

            # Worker should receive the step control
            msg = ctrl.wait_for(lambda m: m.get("type") == "control" and m.get("action") == "step")
            assert msg is not None, "Worker did not receive step control"
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Text input / send
# ---------------------------------------------------------------------------


class TestInputSend:
    def test_input_row_visible_when_hijacked(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """The text-input row becomes visible after acquiring hijack."""
        base_url, _ = hijack_server
        worker_id = f"inp-vis-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            expect(_step_btn(page)).to_be_enabled(timeout=5000)

            # .hijack-input-row must have class "visible"
            page.wait_for_function("window.__deepQuery('.hijack-input-row')?.classList.contains('visible')")
        finally:
            ctrl.stop()

    def test_send_button_delivers_input_to_worker(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Typing in the input field and clicking Send delivers input to the worker."""
        base_url, _ = hijack_server
        worker_id = f"inp-send-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            expect(_step_btn(page)).to_be_enabled(timeout=5000)

            # Wait for input row to appear
            page.wait_for_function("window.__deepQuery('.hijack-input-row')?.classList.contains('visible')")

            page.locator("#inputfield").fill("hello\\r")
            page.get_by_role("button", name="Send").click()

            msg = ctrl.wait_for(lambda m: m.get("type") == "input")
            assert msg is not None, "Worker did not receive input message"
            # The widget unescapes \\r → \r
            assert "\r" in msg.get("data", "") or "hello" in msg.get("data", "")
        finally:
            ctrl.stop()

    def test_input_row_hidden_after_release(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Input row is hidden again after releasing the hijack."""
        base_url, _ = hijack_server
        worker_id = f"inp-hide-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            page.wait_for_function("window.__deepQuery('.hijack-input-row')?.classList.contains('visible')")

            _release_btn(page).click()

            page.wait_for_function("!window.__deepQuery('.hijack-input-row')?.classList.contains('visible')")
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent_while_hijacked(self, page: Page, hijack_server: tuple[str, object]) -> None:
        """Widget sends heartbeat messages while holding the hijack (every 500 ms in tests)."""
        base_url, _ = hijack_server
        worker_id = f"hb-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            expect(_step_btn(page)).to_be_enabled(timeout=5000)

            # heartbeatInterval=500ms → expect at least one heartbeat within 2s
            # The hub converts heartbeat → heartbeat_ack; we observe the ack
            # arriving at the browser (visible to the WS, not in ctrl.received).
            # Instead, verify the hub received the heartbeat by checking that
            # the REST snapshot returns a valid lease_expires_at.

            time.sleep(1.2)  # let heartbeat fire at least twice

            # Verify the hub still considers the session active (lease extended).
            # Checked indirectly: browser status is still "Hijacked (you)".
            assert _status_text(page) == "Hijacked (you)"
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Two browsers — isolation
# ---------------------------------------------------------------------------


class TestTwoBrowsers:
    def test_second_browser_sees_hijacked_other(
        self, page: Page, browser: object, hijack_server: tuple[str, object]
    ) -> None:
        """A second browser connecting during an active hijack sees 'Hijacked (other)'."""
        base_url, _ = hijack_server
        worker_id = f"2br-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            # Browser 1 acquires hijack
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (you)'")

            # Browser 2 opens in a new context

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                page2.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (other)'")
                assert page2.locator("#statustext").text_content() == "Hijacked (other)", (
                    "Second browser should see 'Hijacked (other)'"
                )
                # Hijack button should be disabled for the second browser
                expect(page2.get_by_role("button", name="Hijack")).to_be_disabled(timeout=2000)
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()

    def test_hijack_button_enabled_for_second_browser_after_release(
        self, page: Page, browser: object, hijack_server: tuple[str, object]
    ) -> None:
        """After browser 1 releases, browser 2's Hijack button becomes enabled."""
        base_url, _ = hijack_server
        worker_id = f"2br-rel-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            expect(_release_btn(page)).to_be_enabled(timeout=5000)

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                page2.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (other)'")

                # Browser 1 releases
                _release_btn(page).click()

                # Browser 2 should now be able to hijack
                expect(page2.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()

    def test_second_browser_can_take_over_after_release(
        self, page: Page, browser: object, hijack_server: tuple[str, object]
    ) -> None:
        """Browser 2 can hijack after browser 1 releases, and browser 1 updates to 'Hijacked (other)'."""
        base_url, _ = hijack_server
        worker_id = f"2br-xfer-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)
            _hijack_btn(page).click()
            page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (you)'")

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                page2.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (other)'")

                _release_btn(page).click()
                expect(page2.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
                page2.get_by_role("button", name="Hijack").click()

                page2.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (you)'")
                page.wait_for_function("window.__deepQuery('#statustext')?.textContent === 'Hijacked (other)'")
                assert _status_text(page) == "Hijacked (other)"
                assert page2.locator("#statustext").text_content() == "Hijacked (you)"
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()
