#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Telnet protocol edge cases.

Scenarios
---------
1. Mock server sends bytes that include 0xFF sequences (IAC IAC = literal 0xFF
   in the telnet protocol); the stack must not crash and the browser must still
   receive a snapshot.
2. When the mock telnet server closes the connection mid-session, the uterm
   server must remain accessible and a new browser can still obtain a snapshot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .conftest import (
    ADMIN_H,
    connect_browser,
    drain_for_snapshot,
    wait_for_session_connected,
)

# ---------------------------------------------------------------------------
# 1. 0xFF bytes in the telnet data stream
# ---------------------------------------------------------------------------


async def test_iac_0xff_byte_passthrough(live_xff_telnet_server: Any) -> None:
    """Stack handles 0xFF bytes without crashing; browser receives a snapshot."""
    base_url = live_xff_telnet_server

    await wait_for_session_connected(base_url, "tel1")
    async with connect_browser(base_url, "tel1") as browser:
        snap = await drain_for_snapshot(browser, timeout=5.0)

    assert snap is not None, "browser did not receive a snapshot after the mock server sent 0xFF bytes"
    assert snap.get("type") == "snapshot"


# ---------------------------------------------------------------------------
# 2. Session survives mock telnet server drop
# ---------------------------------------------------------------------------


async def test_session_survives_telnet_server_drop(live_telnet_server: Any) -> None:
    """After the mock telnet server closes, the uterm server remains accessible."""
    base_url, srv = live_telnet_server

    # Confirm the session is running and the browser can get a snapshot
    await wait_for_session_connected(base_url, "tel1")
    async with connect_browser(base_url, "tel1") as browser:
        initial = await drain_for_snapshot(browser, timeout=5.0)
    assert initial is not None, "no snapshot received before mock server drop"

    # Drop the mock telnet server
    srv.close()
    await asyncio.sleep(0.5)

    # The uterm server must still respond to API requests
    async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=5.0) as http:
        resp = await http.get("/api/sessions")

    assert resp.status_code == 200, f"API returned {resp.status_code} after telnet server drop"
    sessions = resp.json()
    assert any(s["session_id"] == "tel1" for s in sessions), f"tel1 not found in sessions after server drop: {sessions}"

    # A new browser can still connect and get a snapshot — the runtime remains
    # connected to the hub even when the underlying telnet transport is down
    async with connect_browser(base_url, "tel1") as browser2:
        after_drop = await drain_for_snapshot(browser2, timeout=10.0)

    assert after_drop is not None, "browser could not get a snapshot after mock server drop"
