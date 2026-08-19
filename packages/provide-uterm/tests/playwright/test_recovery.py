#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright test for hostile client disconnect recovery."""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page

from .ui_routes import install_multi_backend_routes, multi_backend_env


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    if multi_backend_env():
        # spinner_mock=True serves the mock-xterm harness, which is what defines
        # window._widget. Without it multi-backend serves the real UI, where
        # _force_close_ws below has no widget to reach through.
        install_multi_backend_routes(page, spinner_mock=True)
    page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")


def _wait_connected(page: Page, timeout: int = 5000) -> None:
    page.wait_for_function(
        "() => window.__deepQuery('#statustext')?.textContent === 'Connected (watching)'",
        timeout=timeout,
    )


from .test_reconnect_spinner import _force_close_ws


@pytest.mark.playwright
class TestClientRecovery:
    def test_hostile_client_disconnect_recovers(
        self,
        page: Page,
        spinner_server: tuple[str, object],
    ) -> None:
        """Browser WS drops abruptly; UI displays reconnecting state and successfully recovers."""
        base_url, _ = spinner_server
        wid = f"recover-{_uid()}"
        # We navigate and force init term
        _navigate(page, base_url, wid)
        page.wait_for_function(
            "window.__deepQuery('#statustext')?.textContent !== 'Connecting…'",
            timeout=5000,
        )
        page.evaluate("window._widget._ensureTerm()")

        # Force a simulated hostile disconnect of the browser's WebSocket
        _force_close_ws(page)

        # Assert the UI gracefully displays the disconnected telemetry state
        page.wait_for_function(
            "() => {"
            "  const st = window.__deepQuery('#statustext')?.textContent || '';"
            "  return st.includes('Reconnecting') || st === 'Connecting…' || st.includes('Offline');"
            "}",
            timeout=5000,
        )

        # Wait for the reconnect
        page.wait_for_function(
            "window._widget && window._widget._hijackState.ws !== null && window._widget._hijackState.ws.readyState === 1",
            timeout=10000,
        )
