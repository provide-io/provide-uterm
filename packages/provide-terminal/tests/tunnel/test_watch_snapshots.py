#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Textual snapshot tests for the uterm watch TUI.

These tests capture SVG snapshots of the WatchApp in various states.
Run with --snapshot-update to regenerate golden files.

NOTE: snap_compare is synchronous (calls asyncio.run internally),
so these tests must NOT be async.
"""

from __future__ import annotations

import pytest

from provide.terminal.cli._watch_app import WatchApp


@pytest.fixture
def app() -> WatchApp:
    """WatchApp that won't connect a real WebSocket."""
    return WatchApp(
        ws_url="ws://localhost:9999/ws/browser/test/term",
        tunnel_id="test-snapshot",
        initial_layout="horizontal",
    )


class TestWatchAppSnapshots:
    """Visual regression tests using SVG snapshots."""

    def test_empty_app(self, snap_compare: object, app: WatchApp) -> None:
        """Snapshot: app with no requests (initial state)."""
        assert snap_compare(app, terminal_size=(120, 30))  # type: ignore[operator]

    def test_app_with_requests(self, snap_compare: object, app: WatchApp) -> None:
        """Snapshot: app with multiple HTTP exchanges in the table."""

        async def setup(pilot: object) -> None:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/api/health",
                    "headers": {},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 200,
                    "headers": {"content-type": "text/plain"},
                    "body": "ok",
                    "duration_ms": 12.3,
                }
            )
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r2",
                    "method": "POST",
                    "url": "/api/connect",
                    "headers": {"content-type": "application/json"},
                    "body_size": 128,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r2",
                    "status": 201,
                    "headers": {},
                    "body": '{"session_id": "s1"}',
                    "duration_ms": 85.7,
                }
            )
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r3",
                    "method": "DELETE",
                    "url": "/api/sessions/s1",
                    "headers": {},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r3",
                    "status": 404,
                    "headers": {},
                    "body": "not found",
                    "duration_ms": 5.1,
                }
            )

        assert snap_compare(app, terminal_size=(120, 30), run_before=setup)  # type: ignore[operator]

    def test_vertical_layout(self, snap_compare: object, app: WatchApp) -> None:
        """Snapshot: app in vertical layout mode."""

        async def setup(pilot: object) -> None:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/api/test",
                    "headers": {},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 200,
                    "headers": {},
                    "body": "response",
                    "duration_ms": 10.0,
                }
            )
            await pilot.press("l")  # type: ignore[attr-defined]

        assert snap_compare(app, terminal_size=(120, 30), run_before=setup)  # type: ignore[operator]

    def test_method_filter(self, snap_compare: object, app: WatchApp) -> None:
        """Snapshot: app with method filter active."""

        async def setup(pilot: object) -> None:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/api/test",
                    "headers": {},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r2",
                    "method": "POST",
                    "url": "/api/create",
                    "headers": {},
                    "body_size": 50,
                }
            )
            await pilot.press("f")  # type: ignore[attr-defined]

        assert snap_compare(app, terminal_size=(120, 30), run_before=setup)  # type: ignore[operator]
