#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tool metadata, _clean_snapshot, _unescape_keys, and CLI tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from provide.terminal.mcp.server import TOOL_COUNT, _clean_snapshot, _unescape_keys, create_mcp_app

_ANSI_SNAPSHOT: dict[str, Any] = {
    "screen": "\x1b[1;31mHello\x1b[0m World",
    "cursor": {"row": 0, "col": 11},
    "cols": 80,
    "rows": 24,
}


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    async def test_tool_count(self) -> None:
        mcp = create_mcp_app("http://test")
        tools = await mcp.list_tools()
        assert len(tools) == TOOL_COUNT

    async def test_tool_names(self) -> None:
        mcp = create_mcp_app("http://test")
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        expected = {
            "hijack_begin",
            "hijack_heartbeat",
            "hijack_read",
            "hijack_send",
            "hijack_step",
            "hijack_release",
            "session_list",
            "session_status",
            "session_read",
            "session_connect",
            "session_disconnect",
            "session_create",
            "server_health",
            "session_set_mode",
            "worker_input_mode",
            "worker_disconnect",
        }
        assert names == expected

    async def test_all_tools_have_descriptions(self) -> None:
        mcp = create_mcp_app("http://test")
        tools = await mcp.list_tools()
        for t in tools:
            assert t.description, f"{t.name} missing description"

    async def test_mcp_app_name(self) -> None:
        mcp = create_mcp_app("http://test")
        assert mcp.name == "uterm"


# ---------------------------------------------------------------------------
# _clean_snapshot unit tests
# ---------------------------------------------------------------------------


class TestCleanSnapshot:
    def test_text_mode_strips_ansi_and_returns_screen_only(self) -> None:
        result = _clean_snapshot(_ANSI_SNAPSHOT, "text")
        assert "Hello World" in result["screen"]
        assert "\x1b" not in result["screen"]
        assert "cursor" not in result
        assert "cols" not in result
        assert "rows" not in result

    def test_rendered_mode_strips_ansi_keeps_metadata(self) -> None:
        result = _clean_snapshot(_ANSI_SNAPSHOT, "rendered")
        assert "Hello World" in result["screen"]
        assert "\x1b" not in result["screen"]
        assert result["cursor"] == {"row": 0, "col": 11}
        assert result["cols"] == 80
        assert result["rows"] == 24

    def test_raw_mode_returns_unchanged(self) -> None:
        result = _clean_snapshot(_ANSI_SNAPSHOT, "raw")
        assert result is _ANSI_SNAPSHOT
        assert "\x1b" in result["screen"]

    def test_rendered_mode_missing_metadata(self) -> None:
        sparse: dict[str, Any] = {"screen": "hello"}
        result = _clean_snapshot(sparse, "rendered")
        assert result["screen"] == "hello"
        assert "cursor" not in result
        assert "cols" not in result
        assert "rows" not in result

    def test_text_mode_empty_screen(self) -> None:
        result = _clean_snapshot({"screen": ""}, "text")
        assert result == {"screen": ""}

    def test_rendered_mode_partial_metadata(self) -> None:
        """Only cols present, cursor/rows absent."""
        snap: dict[str, Any] = {"screen": "x", "cols": 40}
        result = _clean_snapshot(snap, "rendered")
        assert result["cols"] == 40
        assert "cursor" not in result
        assert "rows" not in result

    def test_text_mode_screen_key_missing(self) -> None:
        """Snapshot dict has no 'screen' key — defaults to empty."""
        result = _clean_snapshot({}, "text")
        assert result == {"screen": ""}

    def test_raw_mode_preserves_extra_keys(self) -> None:
        """Raw mode passes through non-standard keys."""
        snap: dict[str, Any] = {"screen": "x", "custom": 42}
        result = _clean_snapshot(snap, "raw")
        assert result["custom"] == 42

    def test_tail_lines_text_mode(self) -> None:
        snap: dict[str, Any] = {"screen": "line1\nline2\nline3\nline4\nline5"}
        result = _clean_snapshot(snap, "text", tail_lines=2)
        assert result["screen"] == "line4\nline5"

    def test_tail_lines_rendered_mode(self) -> None:
        snap: dict[str, Any] = {"screen": "a\nb\nc\nd", "cols": 80, "rows": 24}
        result = _clean_snapshot(snap, "rendered", tail_lines=2)
        assert result["screen"] == "c\nd"
        assert result["cols"] == 80

    def test_tail_lines_raw_mode(self) -> None:
        snap: dict[str, Any] = {"screen": "\x1b[31mA\x1b[0m\nB\nC", "cols": 80}
        result = _clean_snapshot(snap, "raw", tail_lines=1)
        assert result["screen"] == "C"
        assert result["cols"] == 80

    def test_tail_lines_none_no_trim(self) -> None:
        snap: dict[str, Any] = {"screen": "a\nb\nc"}
        result = _clean_snapshot(snap, "text", tail_lines=None)
        assert result["screen"] == "a\nb\nc"

    def test_tail_lines_larger_than_content(self) -> None:
        snap: dict[str, Any] = {"screen": "a\nb"}
        result = _clean_snapshot(snap, "text", tail_lines=10)
        assert result["screen"] == "a\nb"

    def test_tail_lines_zero_no_trim(self) -> None:
        snap: dict[str, Any] = {"screen": "a\nb\nc"}
        result = _clean_snapshot(snap, "text", tail_lines=0)
        assert result["screen"] == "a\nb\nc"

    def test_tail_lines_raw_larger_than_content(self) -> None:
        snap: dict[str, Any] = {"screen": "\x1b[31mA\x1b[0m\nB", "cols": 80}
        result = _clean_snapshot(snap, "raw", tail_lines=10)
        assert result["screen"] == "\x1b[31mA\x1b[0m\nB"
        assert result["cols"] == 80

    def test_tail_lines_rendered_larger_than_content(self) -> None:
        snap: dict[str, Any] = {"screen": "a\nb", "cols": 80}
        result = _clean_snapshot(snap, "rendered", tail_lines=10)
        assert result["screen"] == "a\nb"


# ---------------------------------------------------------------------------
# _unescape_keys unit tests
# ---------------------------------------------------------------------------


class TestUnescapeKeys:
    def test_cr(self) -> None:
        assert _unescape_keys(r"hello\r") == "hello\r"

    def test_lf(self) -> None:
        assert _unescape_keys(r"hello\n") == "hello\n"

    def test_tab(self) -> None:
        assert _unescape_keys(r"col1\tcol2") == "col1\tcol2"

    def test_escape_x1b(self) -> None:
        assert _unescape_keys(r"\x1b[A") == "\x1b[A"

    def test_escape_e(self) -> None:
        assert _unescape_keys(r"\e[A") == "\x1b[A"

    def test_literal_backslash(self) -> None:
        assert _unescape_keys(r"a\\b") == "a\\b"

    def test_no_escapes(self) -> None:
        assert _unescape_keys("plain text") == "plain text"

    def test_empty_string(self) -> None:
        assert _unescape_keys("") == ""

    def test_multiple_escapes(self) -> None:
        assert _unescape_keys(r"ls\r\necho hi\r") == "ls\r\necho hi\r"

    def test_unknown_escape_passed_through(self) -> None:
        assert _unescape_keys(r"\z") == "\\z"

    def test_trailing_backslash(self) -> None:
        assert _unescape_keys("abc\\") == "abc\\"

    def test_mixed_real_and_escaped(self) -> None:
        """Real newline + backslash-r in same string — \\r is unescaped."""
        assert _unescape_keys("a\nb\\rc") == "a\nb\rc"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_parser_basic(self) -> None:
        from provide.terminal.mcp.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--url", "http://localhost:8780"])
        assert args.url == "http://localhost:8780"
        assert args.entity_prefix == "/worker"
        assert args.headers == []

    def test_parser_all_options(self) -> None:
        from provide.terminal.mcp.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "--url",
                "http://localhost:8780",
                "--entity-prefix",
                "/bot",
                "--header",
                "Authorization:Bearer tok",
                "--header",
                "X-Custom:val",
            ]
        )
        assert args.url == "http://localhost:8780"
        assert args.entity_prefix == "/bot"
        assert len(args.headers) == 2

    def test_main_creates_and_runs(self) -> None:
        from provide.terminal.mcp.cli import main

        mock_app = MagicMock()
        with patch(
            "provide.terminal.mcp.server.create_mcp_app",
            return_value=mock_app,
        ) as mock_create:
            main(["--url", "http://localhost:8780"])

        mock_create.assert_called_once_with(
            "http://localhost:8780",
            entity_prefix="/worker",
            headers=None,
        )
        mock_app.run.assert_called_once_with(transport="stdio")

    def test_main_with_headers_and_prefix(self) -> None:
        from provide.terminal.mcp.cli import main

        mock_app = MagicMock()
        with patch(
            "provide.terminal.mcp.server.create_mcp_app",
            return_value=mock_app,
        ) as mock_create:
            main(
                [
                    "--url",
                    "http://x",
                    "--header",
                    "Auth:Bearer t",
                    "--entity-prefix",
                    "/bot",
                ]
            )

        mock_create.assert_called_once_with(
            "http://x",
            entity_prefix="/bot",
            headers={"Auth": "Bearer t"},
        )
        mock_app.run.assert_called_once_with(transport="stdio")

    def test_main_multiple_headers(self) -> None:
        from provide.terminal.mcp.cli import main

        mock_app = MagicMock()
        with patch(
            "provide.terminal.mcp.server.create_mcp_app",
            return_value=mock_app,
        ) as mock_create:
            main(
                [
                    "--url",
                    "http://x",
                    "--header",
                    "A:1",
                    "--header",
                    "B:2",
                ]
            )

        call_headers = mock_create.call_args.kwargs["headers"]
        assert call_headers == {"A": "1", "B": "2"}


# ---------------------------------------------------------------------------
# Lifespan — HijackClient cleanup
# ---------------------------------------------------------------------------


class TestLifespan:
    async def test_lifespan_closes_hijack_client(self) -> None:
        """FastMCP lifespan hook calls HijackClient.__aexit__ on shutdown."""
        from unittest.mock import AsyncMock

        with patch(
            "provide.terminal.mcp.server.HijackClient",
        ) as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.__aexit__ = AsyncMock()
            mcp = create_mcp_app("http://test")

        # Simulate the MCP lifespan cycle
        async with mcp._lifespan_manager():
            pass

        mock_instance.__aexit__.assert_awaited_once_with(None, None, None)
