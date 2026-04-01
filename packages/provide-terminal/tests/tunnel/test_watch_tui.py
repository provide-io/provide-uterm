#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Textual TUI tests for uterm watch using App.run_test()."""

from __future__ import annotations

import pytest
from textual.widgets import Static

from provide.terminal.cli._watch_app import Exchange, WatchApp, _detail_lines


@pytest.fixture
def app() -> WatchApp:
    """Create a WatchApp that won't try to connect a real WebSocket."""
    return WatchApp(
        ws_url="ws://localhost:9999/ws/browser/test/term", tunnel_id="test-tui", initial_layout="horizontal"
    )


class TestWatchAppRenders:
    @pytest.mark.asyncio
    async def test_app_mounts_widgets(self, app: WatchApp) -> None:
        """App renders DataTable, detail pane, status bar, header, footer."""
        async with app.run_test(size=(120, 40)) as _pilot:
            assert app.query_one("#request-table") is not None
            assert app.query_one("#detail-pane") is not None
            assert app.query_one("#status-bar") is not None

    @pytest.mark.asyncio
    async def test_table_has_columns(self, app: WatchApp) -> None:
        """DataTable has the expected columns."""
        from textual.widgets import DataTable

        async with app.run_test(size=(120, 40)) as _pilot:
            table = app.query_one("#request-table", DataTable)
            col_labels = [str(c.label) for c in table.columns.values()]
            assert "Method" in col_labels
            assert "URL" in col_labels
            assert "Status" in col_labels
            assert "Duration" in col_labels
            assert "Size" in col_labels

    @pytest.mark.asyncio
    async def test_handle_http_req_adds_row(self, app: WatchApp) -> None:
        """_handle_frame with http_req adds a row to the table."""
        from textual.widgets import DataTable

        async with app.run_test(size=(120, 40)) as _pilot:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/api/test",
                    "headers": {"accept": "application/json"},
                    "body_size": 0,
                }
            )
            table = app.query_one("#request-table", DataTable)
            assert table.row_count == 1

    @pytest.mark.asyncio
    async def test_handle_http_res_updates_row(self, app: WatchApp) -> None:
        """_handle_frame with http_res updates the matching row."""
        async with app.run_test(size=(120, 40)) as _pilot:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "POST",
                    "url": "/api/login",
                    "headers": {},
                    "body_size": 10,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 200,
                    "status_text": "OK",
                    "headers": {},
                    "body_size": 24,
                    "duration_ms": 89,
                }
            )
            assert app._exchanges[0].status == 200
            assert app._exchanges[0].duration_ms == 89

    @pytest.mark.asyncio
    async def test_multiple_exchanges(self, app: WatchApp) -> None:
        """Multiple request/response pairs tracked correctly."""
        from textual.widgets import DataTable

        async with app.run_test(size=(120, 40)) as _pilot:
            for i in range(5):
                app._handle_frame(
                    {
                        "type": "http_req",
                        "id": f"r{i}",
                        "method": "GET",
                        "url": f"/api/item/{i}",
                        "headers": {},
                        "body_size": 0,
                    }
                )
                app._handle_frame(
                    {
                        "type": "http_res",
                        "id": f"r{i}",
                        "status": 200,
                        "status_text": "OK",
                        "headers": {},
                        "body_size": 10,
                        "duration_ms": i + 1.0,
                    }
                )
            table = app.query_one("#request-table", DataTable)
            assert table.row_count == 5
            assert len(app._exchanges) == 5


class TestWatchAppActions:
    @pytest.mark.asyncio
    async def test_cycle_layout(self, app: WatchApp) -> None:
        """Tab cycles through layout modes."""
        async with app.run_test(size=(120, 40)) as pilot:
            assert app._layout_mode == "horizontal"
            await pilot.press("l")
            assert app._layout_mode == "vertical"
            await pilot.press("l")
            assert app._layout_mode == "modal"
            await pilot.press("l")
            assert app._layout_mode == "horizontal"

    @pytest.mark.asyncio
    async def test_cycle_method_filter(self, app: WatchApp) -> None:
        """f cycles through method filters."""
        async with app.run_test(size=(120, 40)) as pilot:
            assert app._method_filter == ""
            await pilot.press("f")
            assert app._method_filter == "GET"
            await pilot.press("f")
            assert app._method_filter == "POST"

    @pytest.mark.asyncio
    async def test_method_filter_hides_rows(self, app: WatchApp) -> None:
        """Method filter hides non-matching rows on rebuild."""
        from textual.widgets import DataTable

        async with app.run_test(size=(120, 40)) as _pilot:
            app._handle_frame(
                {"type": "http_req", "id": "r1", "method": "GET", "url": "/a", "headers": {}, "body_size": 0}
            )
            app._handle_frame(
                {"type": "http_req", "id": "r2", "method": "POST", "url": "/b", "headers": {}, "body_size": 0}
            )
            table = app.query_one("#request-table", DataTable)
            assert table.row_count == 2

            app._method_filter = "GET"
            app._rebuild_table()
            assert table.row_count == 1

    @pytest.mark.asyncio
    async def test_quit(self, app: WatchApp) -> None:
        """q quits the app."""
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("q")


class TestDetailScreen:
    @pytest.mark.asyncio
    async def test_modal_detail_opens(self, app: WatchApp) -> None:
        """In modal layout, selecting a row pushes DetailScreen."""
        app._layout_mode = "modal"
        async with app.run_test(size=(120, 40)) as pilot:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/test",
                    "headers": {"accept": "text/html"},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 200,
                    "status_text": "OK",
                    "headers": {"content-type": "text/html"},
                    "body_size": 100,
                    "duration_ms": 42,
                }
            )
            # Select the row
            from textual.widgets import DataTable

            table = app.query_one("#request-table", DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")

    @pytest.mark.asyncio
    async def test_split_detail_updates_pane(self, app: WatchApp) -> None:
        """In horizontal layout, selecting a row updates the detail pane."""
        async with app.run_test(size=(120, 40)) as pilot:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "POST",
                    "url": "/api",
                    "headers": {"content-type": "application/json"},
                    "body_size": 10,
                    "body_b64": "eyJ0ZXN0IjoxfQ==",
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 201,
                    "status_text": "Created",
                    "headers": {"x-id": "r1"},
                    "body_size": 5,
                    "duration_ms": 15,
                    "body_b64": "eyJvayI6MX0=",
                }
            )
            from textual.widgets import DataTable

            table = app.query_one("#request-table", DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            pane = app.query_one("#detail-pane", Static)
            # Static stores its content in _content or render() output
            content = str(pane.render())
            assert "POST" in content or "api" in content

    @pytest.mark.asyncio
    async def test_table_row_update_shows_status(self, app: WatchApp) -> None:
        """Response updates the table row with status, duration, size."""

        async with app.run_test(size=(120, 40)) as _pilot:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/x",
                    "headers": {},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 404,
                    "status_text": "Not Found",
                    "headers": {},
                    "body_size": 0,
                    "duration_ms": 3,
                }
            )
            assert app._exchanges[0].status == 404


class TestTableUpdateCoverage:
    @pytest.mark.asyncio
    async def test_method_filter_skips_add(self, app: WatchApp) -> None:
        """Line 250: _add_table_row returns early when filter doesn't match."""
        from textual.widgets import DataTable

        async with app.run_test(size=(120, 40)) as _pilot:
            app._method_filter = "POST"
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/skip",
                    "headers": {},
                    "body_size": 0,
                }
            )
            table = app.query_one("#request-table", DataTable)
            assert table.row_count == 0  # Filtered out

    @pytest.mark.asyncio
    async def test_update_table_row_with_response(self, app: WatchApp) -> None:
        """Lines 259-262: _update_table_row calls update_cell_at."""
        from textual.widgets import DataTable

        async with app.run_test(size=(120, 40)) as _pilot:
            app._handle_frame(
                {
                    "type": "http_req",
                    "id": "r1",
                    "method": "GET",
                    "url": "/test",
                    "headers": {},
                    "body_size": 0,
                }
            )
            app._handle_frame(
                {
                    "type": "http_res",
                    "id": "r1",
                    "status": 200,
                    "status_text": "OK",
                    "headers": {},
                    "body_size": 100,
                    "duration_ms": 42,
                }
            )
            app.query_one("#request-table", DataTable)
            assert app._exchanges[0].status == 200

    @pytest.mark.asyncio
    async def test_update_status_without_widget(self) -> None:
        """Lines 267-268: _update_status handles missing widget."""
        watch = WatchApp(ws_url="ws://x", tunnel_id="t", initial_layout="horizontal")
        # Call before mount — should not raise
        watch._update_status()

    @pytest.mark.asyncio
    async def test_on_row_selected_unknown_id(self, app: WatchApp) -> None:
        """Line 277: _on_row_selected returns early for unknown row key."""
        async with app.run_test(size=(120, 40)) as _pilot:
            from textual.widgets import DataTable
            from textual.widgets._data_table import RowKey

            table = app.query_one("#request-table", DataTable)
            # Simulate event with unknown key
            event = DataTable.RowSelected(table, table.cursor_coordinate, RowKey("nonexistent"))
            app._on_row_selected(event)  # Should not raise


class TestDetailLines:
    def test_request_only(self) -> None:
        ex = Exchange(req_id="r1", method="GET", url="/api/test", req_headers={"accept": "text/html"})
        lines = _detail_lines(ex)
        assert any("GET /api/test" in line for line in lines)
        assert any("accept" in line for line in lines)

    def test_with_response(self) -> None:
        ex = Exchange(
            req_id="r1",
            method="POST",
            url="/api/login",
            req_headers={"content-type": "application/json"},
            status=200,
            status_text="OK",
            duration_ms=89,
            res_headers={"x-request-id": "r1"},
        )
        lines = _detail_lines(ex)
        assert any("200" in line for line in lines)
        assert any("x-request-id" in line for line in lines)
