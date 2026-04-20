#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Full-stack browser WS → FastAPI → TelnetConnector → mock telnet server.

Scenarios
---------
1. Browser connects to an auto-start telnet session; receives a snapshot that
   includes the session header (proves the full auto-start stack works).
2. Mock telnet echo server sends a recognisable banner; the browser eventually
   sees it in the snapshot screen buffer.
3. Two concurrent auto-start telnet sessions remain isolated: each browser
   sees only its own session's screen content.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .conftest import (
    connect_browser,
    drain_for_snapshot,
    drain_for_snapshot_with_text,
    wait_for_session_connected,
)

# ---------------------------------------------------------------------------
# 1. Browser receives a snapshot from an auto-start telnet session
# ---------------------------------------------------------------------------


async def test_telnet_browser_receives_snapshot(live_telnet_server: Any) -> None:
    """Browser connects to auto-start telnet session and receives a snapshot."""
    base_url, _srv = live_telnet_server

    await wait_for_session_connected(base_url, "tel1")
    async with connect_browser(base_url, "tel1") as browser:
        snap = await drain_for_snapshot(browser, timeout=5.0)

    assert snap is not None, "browser never received a snapshot from the telnet session"
    screen = snap.get("screen", "")
    assert "telnet://" in screen, f"snapshot screen missing 'telnet://': {screen!r}"


# ---------------------------------------------------------------------------
# 2. Screen buffer contains data sent by the mock echo server
# ---------------------------------------------------------------------------


async def test_telnet_screen_shows_received_data(live_telnet_server: Any) -> None:
    """Browser snapshot screen reflects the banner text sent by the mock echo server."""
    base_url, _srv = live_telnet_server

    await wait_for_session_connected(base_url, "tel1")
    async with connect_browser(base_url, "tel1") as browser:
        snap = await drain_for_snapshot_with_text(browser, "ECHO_BANNER", timeout=10.0)

    assert snap is not None, (
        "browser never received a snapshot containing 'ECHO_BANNER'; "
        "the mock echo server data did not reach the screen buffer"
    )
    assert "ECHO_BANNER" in snap.get("screen", "")


# ---------------------------------------------------------------------------
# 3. Two concurrent telnet sessions remain isolated
# ---------------------------------------------------------------------------


async def test_two_concurrent_sessions_isolated(live_two_telnet_server: Any) -> None:
    """Two browsers connected to different sessions see only their own screens."""
    base_url = live_two_telnet_server

    await asyncio.gather(
        wait_for_session_connected(base_url, "tel1"),
        wait_for_session_connected(base_url, "tel2"),
    )

    async with (
        connect_browser(base_url, "tel1") as b1,
        connect_browser(base_url, "tel2") as b2,
    ):
        snap1, snap2 = await asyncio.gather(
            drain_for_snapshot(b1, timeout=5.0),
            drain_for_snapshot(b2, timeout=5.0),
        )

    assert snap1 is not None, "tel1 browser did not receive a snapshot"
    assert snap2 is not None, "tel2 browser did not receive a snapshot"

    screen1 = snap1.get("screen", "")
    screen2 = snap2.get("screen", "")

    # Each snapshot must identify its own session by name or id
    assert "tel1" in screen1 or "Telnet One" in screen1, f"tel1 snapshot does not identify session: {screen1!r}"
    assert "tel2" in screen2 or "Telnet Two" in screen2, f"tel2 snapshot does not identify session: {screen2!r}"
    # Screens must differ — same content would indicate cross-session leakage
    assert screen1 != screen2, "tel1 and tel2 snapshots are identical — isolation may be broken"
