#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Playwright smoke tests for the ProvideHijack widget."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEMO_PORT = 18765
DEMO_URL = f"http://127.0.0.1:{DEMO_PORT}"
_PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def demo_server():
    """Start the demo_server.py subprocess and wait for it to be ready."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.demo_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(DEMO_PORT),
        ],
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    yield proc
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mobile_keys_toolbar_button_present(demo_server) -> None:  # noqa: ANN001
    """⌨ toggle button must be visible in the hijack widget toolbar."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page: Page = browser.new_page()
        page.goto(f"{DEMO_URL}/hijack/hijack.html?bot=testbot")
        page.wait_for_selector("#app", timeout=5000)
        btn = page.locator('button[title="Mobile key toolbar"]')
        assert btn.count() == 1, "⌨ mobile-keys toggle button not found in toolbar"
        browser.close()


def test_mobile_keys_row_hidden_before_hijack(demo_server) -> None:  # noqa: ANN001
    """The .mobile-keys row must not be visible before the hijack is acquired."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page: Page = browser.new_page()
        page.goto(f"{DEMO_URL}/hijack/hijack.html?bot=testbot")
        page.wait_for_selector("#app", timeout=5000)

        # Click the ⌨ toggle — row stays hidden until hijack is acquired
        page.locator('button[title="Mobile key toolbar"]').click()

        row = page.locator(".mobile-keys")
        assert not row.is_visible(), ".mobile-keys row must not be visible before hijack is acquired"
        browser.close()
