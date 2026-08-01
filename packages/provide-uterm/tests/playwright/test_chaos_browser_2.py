#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright browser stress tests: visibility, rapid refresh, throttle, close race.

Scenarios
---------
25. Tab backgrounded during active hijack — heartbeat survives or recovers.
26. Rapid page refresh 10 times during hijack — no orphaned WS.
27. Network throttle (CDP) during hijack acquire — widget handles slow connection.
28. WebSocket close code race — browser release + worker crash simultaneous.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from ..conftest import WorkerController  # noqa: TID252
from .ui_routes import install_multi_backend_routes, multi_backend_env


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    # Multi-backend subprocess servers do not ship /test-page; fulfill via page.route.
    if multi_backend_env():
        install_multi_backend_routes(page)
    page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")


def _hijack_btn(page: Page) -> object:
    return page.get_by_role("button", name="Hijack")


def _release_btn(page: Page) -> object:
    return page.get_by_role("button", name="Release")


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


def _wait_settled_after_reconnect(page: Page, timeout: int = 15000) -> str:
    """Wait until the reconnect handshake has settled; return the final status.

    A browser that reconnects inside the resume-token TTL may legitimately
    reclaim the hijack lease it held before the drop (the disconnect marks the
    resume token as the hijack owner and the ``resume`` frame reclaims it), so
    ``Connected (watching)`` is *not* the guaranteed outcome — ``Hijacked
    (you)`` is equally correct.  Which one lands depends on backend-internal
    ordering between the disconnect bookkeeping and the resume frame.

    The status is only trusted once it has held for a full second: the widget
    shows ``Connected (watching)`` from the fresh hello *before* a pending
    reclaim lands, so sampling too early would race the handshake.
    """
    page.wait_for_function(
        """() => {
            const st = window.__deepQuery('#statustext')?.textContent || '';
            if (st !== 'Connected (watching)' && st !== 'Hijacked (you)') {
                window.__settleStatus = null;
                return false;
            }
            const now = Date.now();
            if (window.__settleStatus !== st) {
                window.__settleStatus = st;
                window.__settleSince = now;
                return false;
            }
            return now - window.__settleSince >= 1000;
        }""",
        timeout=timeout,
    )
    return _status_text(page)


def _wait_stable_state(page: Page, timeout: int = 10000) -> None:
    """Wait until status text is a known stable state (not transitioning)."""
    page.wait_for_function(
        """() => {
            const st = window.__deepQuery('#statustext')?.textContent || '';
            return st === 'Connected (watching)' || st === 'Hijacked (you)'
                || st === 'Offline' || st === 'Disconnected'
                || st === 'Connected (shared)';
        }""",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 25. Tab backgrounded during active hijack
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestTabBackgroundedDuringHijack:
    def test_tab_backgrounded_during_active_hijack(
        self,
        page: Page,
        hijack_server: tuple[str, object],
    ) -> None:
        """Mock visibility change during hijack; widget recovers after foregrounding."""
        base_url, _ = hijack_server
        wid = f"bg-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        # Capture JS console errors
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            _navigate(page, base_url, wid)
            _wait_connected(page)

            # Acquire hijack
            _hijack_btn(page).click()  # type: ignore[union-attr]
            _wait_hijacked_by_me(page)

            # Simulate tab background
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {value: true, configurable: true, writable: true});
                Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true, writable: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")

            # Wait 3 seconds while "backgrounded"
            page.wait_for_timeout(3000)

            # Simulate tab foreground
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {value: false, configurable: true, writable: true});
                Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true, writable: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")

            # Widget should recover to a usable state — either still hijacked
            # or gracefully released (heartbeat may or may not survive background)
            _wait_stable_state(page, timeout=10000)
            status = _status_text(page)
            assert status in ("Connected (watching)", "Hijacked (you)", "Hijacked (other)"), (
                f"Widget should recover after foregrounding, got: {status}"
            )

            # If hijack was lost, re-acquire to prove widget is functional
            if status == "Connected (watching)":
                _hijack_btn(page).click()  # type: ignore[union-attr]
                _wait_hijacked_by_me(page)

            # No JS console errors (ignore resource loading and WebSocket close noise)
            real_errors = [
                e
                for e in console_errors
                if "favicon" not in e.lower()
                and "websocket" not in e.lower()
                and "failed to load resource" not in e.lower()
            ]
            assert len(real_errors) == 0, f"JS console errors: {real_errors}"

        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# 26. Rapid page refresh 10 times during hijack
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestRapidPageRefresh:
    def test_rapid_page_refresh_10_times_during_hijack(
        self,
        page: Page,
        hijack_server: tuple[str, object],
    ) -> None:
        """10 rapid reloads after hijack; widget reaches stable state, no orphaned WS."""
        base_url, _ = hijack_server
        wid = f"refresh-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        try:
            _navigate(page, base_url, wid)
            _wait_connected(page)

            # Acquire hijack
            _hijack_btn(page).click()  # type: ignore[union-attr]
            _wait_hijacked_by_me(page)

            # Rapid reload 10 times
            for _ in range(10):
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(100)

            # After the final reload the widget settles into one of two live
            # states: a plain watcher, or the resumed lease owner (the resume
            # token carries hijack ownership across the drop).  Both are
            # correct; an orphaned socket would settle into neither.
            settled = _wait_settled_after_reconnect(page)

            # Verify the widget is functional whichever way it landed: an owner
            # must be able to release the lease it reclaimed, and the resulting
            # watcher must be able to acquire a fresh one.
            if settled == "Hijacked (you)":
                _release_btn(page).click()  # type: ignore[union-attr]
                _wait_connected(page, timeout=10000)
            expect(_hijack_btn(page)).to_be_enabled(timeout=5000)  # type: ignore[union-attr]
            _hijack_btn(page).click()  # type: ignore[union-attr]
            _wait_hijacked_by_me(page)

        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# 27. Network throttle (CDP) during hijack acquire
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestNetworkThrottleDuringHijack:
    def test_network_throttle_during_hijack_acquire(
        self,
        page: Page,
        hijack_server: tuple[str, object],
    ) -> None:
        """Slow network during hijack acquire; widget doesn't hang indefinitely."""
        base_url, _ = hijack_server
        wid = f"throttle-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            _navigate(page, base_url, wid)
            _wait_connected(page)

            # Enable network throttling via CDP (very slow: 6400 bytes/sec ≈ 50kbps)
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "downloadThroughput": 6400,
                    "uploadThroughput": 6400,
                    "latency": 500,
                },
            )

            # Click hijack under throttled conditions
            _hijack_btn(page).click()  # type: ignore[union-attr]

            # Wait up to 15 seconds for a stable state (not hung)
            _wait_stable_state(page, timeout=15000)

            # Remove throttle
            cdp.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "downloadThroughput": -1,
                    "uploadThroughput": -1,
                    "latency": 0,
                },
            )

            # After throttle removed, widget should recover
            page.wait_for_timeout(2000)
            _wait_stable_state(page, timeout=10000)

            # No unhandled promise rejections
            promise_errors = [e for e in console_errors if "unhandled" in e.lower() or "rejection" in e.lower()]
            assert len(promise_errors) == 0, f"Unhandled promise rejections: {promise_errors}"

        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# 28. WebSocket close code race — browser release + worker crash
# ---------------------------------------------------------------------------


@pytest.mark.playwright
class TestWebSocketCloseCodeRace:
    def test_ws_close_race_browser_release_worker_crash(
        self,
        page: Page,
        hijack_server: tuple[str, object],
    ) -> None:
        """Release + worker crash at same instant; widget recovers to Offline."""
        base_url, _ = hijack_server
        wid = f"close-race-{_uid()}"
        ctrl = WorkerController(base_url, wid).start()

        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            _navigate(page, base_url, wid)
            _wait_connected(page)

            # Acquire hijack
            _hijack_btn(page).click()  # type: ignore[union-attr]
            _wait_hijacked_by_me(page)

            # Click release and kill worker nearly simultaneously
            _release_btn(page).click()  # type: ignore[union-attr]
            ctrl.stop()

            # Widget should reach Offline or Disconnected
            page.wait_for_function(
                """() => {
                    const st = window.__deepQuery('#statustext')?.textContent || '';
                    return st === 'Offline' || st === 'Disconnected';
                }""",
                timeout=10000,
            )

            # Hijack button should be disabled (no worker)
            expect(_hijack_btn(page)).to_be_disabled(timeout=3000)  # type: ignore[union-attr]

            # New worker → widget recovers
            ctrl2 = WorkerController(base_url, wid).start()
            try:
                _wait_connected(page, timeout=10000)
                expect(_hijack_btn(page)).to_be_enabled(timeout=5000)  # type: ignore[union-attr]
            finally:
                ctrl2.stop()

            # No JS errors
            real_errors = [e for e in console_errors if "favicon" not in e.lower()]
            assert len(real_errors) == 0, f"JS console errors: {real_errors}"

        except Exception:
            ctrl.stop()
            raise
