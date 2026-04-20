#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Behavioral tests for the uterm watch TUI (WatchApp).

Replaced pixel-exact SVG snapshot tests with structural assertions so the
suite is portable across environments without font/DPI-sensitive baselines.
"""

from __future__ import annotations

import pytest

from provide.terminal.cli._watch_app import WatchApp
from textual.widgets import DataTable


@pytest.fixture
def app() -> WatchApp:
    """WatchApp configured to not connect a real WebSocket."""
    return WatchApp(
        ws_url="ws://localhost:9999/ws/browser/test/term",
        tunnel_id="test-snapshot",
        initial_layout="horizontal",
    )


_REQ_GET = {
    "type": "http_req",
    "id": "r1",
    "method": "GET",
    "url": "/api/health",
    "headers": {},
    "body_size": 0,
}
_RES_200 = {
    "type": "http_res",
    "id": "r1",
    "status": 200,
    "headers": {"content-type": "text/plain"},
    "body": "ok",
    "duration_ms": 12.3,
}
_REQ_POST = {
    "type": "http_req",
    "id": "r2",
    "method": "POST",
    "url": "/api/connect",
    "headers": {"content-type": "application/json"},
    "body_size": 128,
}
_RES_201 = {
    "type": "http_res",
    "id": "r2",
    "status": 201,
    "headers": {},
    "body": '{"session_id": "s1"}',
    "duration_ms": 85.7,
}
_REQ_DELETE = {
    "type": "http_req",
    "id": "r3",
    "method": "DELETE",
    "url": "/api/sessions/s1",
    "headers": {},
    "body_size": 0,
}
_RES_404 = {
    "type": "http_res",
    "id": "r3",
    "status": 404,
    "headers": {},
    "body": "not found",
    "duration_ms": 5.1,
}


class TestWatchAppBehavior:
    """Structural/behavioral tests — no SVG baseline required."""

    async def test_empty_app_initial_state(self, app: WatchApp) -> None:
        """Initial state: table has correct columns and zero rows; app state reflects Disconnected."""
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#request-table", DataTable)
            col_labels = [str(c.label) for c in table.columns.values()]
            assert col_labels == ["Method", "URL", "Status", "Duration", "Size"]
            assert table.row_count == 0
            assert app._connected is False
            assert app._request_count == 0
            assert app._tunnel_id == "test-snapshot"

    async def test_app_with_requests(self, app: WatchApp) -> None:
        """After feeding request/response frames, table rows reflect the data."""
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for frame in (_REQ_GET, _RES_200, _REQ_POST, _RES_201, _REQ_DELETE, _RES_404):
                app._handle_frame(frame)
            await pilot.pause()
            table = app.query_one("#request-table", DataTable)
            assert table.row_count == 3
            assert app._request_count == 3
            # Verify status and duration were applied to completed exchanges
            ex_get = next(e for e in app._exchanges if e.req_id == "r1")
            assert ex_get.status == 200
            assert ex_get.duration_ms == pytest.approx(12.3)
            ex_del = next(e for e in app._exchanges if e.req_id == "r3")
            assert ex_del.status == 404

    async def test_vertical_layout_toggle(self, app: WatchApp) -> None:
        """Pressing 'l' cycles the layout mode through horizontal → vertical → modal → horizontal."""
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app._layout_mode == "horizontal"
            await pilot.press("l")
            assert app._layout_mode == "vertical"
            await pilot.press("l")
            assert app._layout_mode == "modal"
            await pilot.press("l")
            assert app._layout_mode == "horizontal"

    async def test_method_filter_cycle(self, app: WatchApp) -> None:
        """Pressing 'f' cycles through method filters and rebuilds the table."""
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for frame in (_REQ_GET, _RES_200, _REQ_POST, _RES_201):
                app._handle_frame(frame)
            await pilot.pause()
            table = app.query_one("#request-table", DataTable)
            assert table.row_count == 2  # GET + POST shown unfiltered

            await pilot.press("f")  # filter = GET
            assert app._method_filter == "GET"
            assert table.row_count == 1

            await pilot.press("f")  # filter = POST
            assert app._method_filter == "POST"
            assert table.row_count == 1

            # Cycle through remaining methods until filter clears
            for _ in range(6):
                await pilot.press("f")
            assert app._method_filter == ""
            assert table.row_count == 2
