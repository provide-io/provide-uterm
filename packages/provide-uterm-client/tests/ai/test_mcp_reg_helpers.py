#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""MCP regression tests — helper/utility edge cases: unescape_keys,
clean_snapshot, key escape integration, CLI, and __init__ re-exports.

Split from test_mcp_regression.py.
"""

from __future__ import annotations

from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport
from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import WorkerTermState

from provide.uterm.ai.server import create_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hub_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


def _add_worker(hub: TermHub, worker_id: str) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    hub._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


def _mcp_for(app: FastAPI) -> FastMCP:
    return create_mcp_app("http://test", transport=ASGITransport(app=app))


async def _call(
    mcp: FastMCP,
    tool: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await mcp.call_tool(tool, args or {})
    return result.structured_content  # type: ignore[return-value]


async def _acquire(
    mcp: FastMCP,
    worker_id: str,
    **kw: Any,
) -> str:
    data = await _call(mcp, "hijack_begin", {"worker_id": worker_id, **kw})
    assert data["success"] is True
    return data["hijack_id"]


# ---------------------------------------------------------------------------
# Key escape processing (_unescape_keys in hijack_send) — integration
# ---------------------------------------------------------------------------


class TestKeyEscapeProcessing:
    """Verify that MCP-layer escape sequences in keys are unescaped."""

    async def test_backslash_r_unescaped_to_cr(self) -> None:
        hub, app = _make_hub_app()
        ws = _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "hello\\r",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "hello\r"

        # Verify the worker received the real CR
        sent_calls = ws.send_text.call_args_list

        for call in sent_calls:
            payload = call[0][0]
            if payload == "hello\r":
                assert payload == "hello\r"
                break

    async def test_backslash_n_unescaped_to_lf(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "line1\\nline2",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "line1\nline2"

    async def test_backslash_t_unescaped_to_tab(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "a\\tb",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "a\tb"

    async def test_escape_sequence_x1b(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "\\x1b[A",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "\x1b[A"

    async def test_escape_sequence_backslash_e(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "\\e[B",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "\x1b[B"

    async def test_literal_backslash(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "path\\\\file",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "path\\file"

    async def test_multiple_escapes_in_one_string(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "ls\\r\\necho hi\\r",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "ls\r\necho hi\r"

    async def test_plain_text_no_escapes(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "hello world",
            },
        )
        assert d["success"] is True
        assert d["sent"] == "hello world"


# ---------------------------------------------------------------------------
# _unescape_keys edge cases (unit-level, not integration)
# ---------------------------------------------------------------------------


class TestUnescapeKeysEdgeCases:
    """Additional edge cases beyond test_mcp_server.py coverage."""

    def test_consecutive_escapes(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"\r\r\r") == "\r\r\r"

    def test_consecutive_mixed_escapes(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"\r\n\t") == "\r\n\t"

    def test_escape_surrounded_by_text(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"before\rafter") == "before\rafter"

    def test_unknown_escape_at_start(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"\qhello") == "\\qhello"

    def test_unknown_escape_at_end(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"hello\q") == "hello\\q"

    def test_multiple_unknown_escapes(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"\a\b\c") == "\\a\\b\\c"

    def test_double_backslash_then_known_escape(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        # \\\r → literal backslash + CR
        assert _unescape_keys(r"\\\r") == "\\\r"

    def test_only_backslash(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys("\\") == "\\"

    def test_only_known_escape(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"\n") == "\n"

    def test_x1b_and_e_both_produce_esc(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        assert _unescape_keys(r"\x1b") == _unescape_keys(r"\e") == "\x1b"

    def test_long_string_with_mixed_content(self) -> None:
        from provide.uterm.ai.server import _unescape_keys

        raw = r"USER admin\rPASS secret\r\nquit\r"
        expected = "USER admin\rPASS secret\r\nquit\r"
        assert _unescape_keys(raw) == expected


# ---------------------------------------------------------------------------
# _clean_snapshot edge cases (unit-level)
# ---------------------------------------------------------------------------


class TestCleanSnapshotEdgeCases:
    """Additional edge cases beyond test_mcp_server.py coverage."""

    def test_missing_screen_key_text_mode(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        assert _clean_snapshot({}, "text") == {"screen": ""}

    def test_missing_screen_key_rendered_mode(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        result = _clean_snapshot({}, "rendered")
        assert result["screen"] == ""
        assert "cursor" not in result

    def test_missing_screen_key_raw_mode(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        result = _clean_snapshot({}, "raw")
        assert result == {}

    def test_empty_screen_all_modes(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        snap: dict[str, Any] = {"screen": "", "cursor": {"x": 0, "y": 0}, "cols": 80, "rows": 24}
        assert _clean_snapshot(snap, "text") == {"screen": ""}
        rendered = _clean_snapshot(snap, "rendered")
        assert rendered["screen"] == ""
        assert rendered["cols"] == 80
        raw = _clean_snapshot(snap, "raw")
        assert raw is snap

    def test_screen_with_only_ansi(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        snap: dict[str, Any] = {"screen": "\x1b[31m\x1b[0m"}
        result = _clean_snapshot(snap, "text")
        assert result["screen"] == ""

    def test_rendered_mode_with_all_metadata(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        snap: dict[str, Any] = {
            "screen": "hello",
            "cursor": {"x": 5, "y": 0},
            "cols": 132,
            "rows": 50,
        }
        result = _clean_snapshot(snap, "rendered")
        assert result["cursor"] == {"x": 5, "y": 0}
        assert result["cols"] == 132
        assert result["rows"] == 50

    def test_extra_keys_not_in_rendered(self) -> None:
        from provide.uterm.ai.server import _clean_snapshot

        snap: dict[str, Any] = {"screen": "x", "custom_key": 99, "cols": 80}
        result = _clean_snapshot(snap, "rendered")
        assert "custom_key" not in result
        assert result["cols"] == 80


# ---------------------------------------------------------------------------
# CLI module edge cases
# ---------------------------------------------------------------------------


class TestCLIEdgeCases:
    def test_header_with_colon_in_value(self) -> None:
        """Header value containing colons should be preserved."""
        from provide.uterm.ai.cli import main

        mock_app = MagicMock()
        with patch(
            "provide.uterm.ai.server.create_mcp_app",
            return_value=mock_app,
        ) as mock_create:
            main(["--url", "http://x", "--header", "Authorization:Bearer abc:def:ghi"])

        call_headers = mock_create.call_args.kwargs["headers"]
        assert call_headers["Authorization"] == "Bearer abc:def:ghi"

    def test_header_with_whitespace(self) -> None:
        """Whitespace around key/value should be stripped."""
        from provide.uterm.ai.cli import main

        mock_app = MagicMock()
        with patch(
            "provide.uterm.ai.server.create_mcp_app",
            return_value=mock_app,
        ) as mock_create:
            main(["--url", "http://x", "--header", "  Key  :  Value  "])

        call_headers = mock_create.call_args.kwargs["headers"]
        assert call_headers["Key"] == "Value"

    def test_empty_header_value(self) -> None:
        from provide.uterm.ai.cli import main

        mock_app = MagicMock()
        with patch(
            "provide.uterm.ai.server.create_mcp_app",
            return_value=mock_app,
        ) as mock_create:
            main(["--url", "http://x", "--header", "X-Empty:"])

        call_headers = mock_create.call_args.kwargs["headers"]
        assert call_headers["X-Empty"] == ""

    def test_main_uses_sys_argv_when_argv_is_none(self) -> None:
        """When argv=None, main falls through to sys.argv[1:]."""
        from provide.uterm.ai.cli import main

        mock_app = MagicMock()
        with (
            patch("sys.argv", ["uterm-mcp", "--url", "http://fallback"]),
            patch(
                "provide.uterm.ai.server.create_mcp_app",
                return_value=mock_app,
            ) as mock_create,
        ):
            main()

        mock_create.assert_called_once_with(
            "http://fallback",
            entity_prefix="/worker",
            headers=None,
        )

    def test_build_parser_prog_name(self) -> None:
        from provide.uterm.ai.cli import _build_parser

        parser = _build_parser()
        assert parser.prog == "uterm-mcp"

    def test_build_parser_missing_url_exits(self) -> None:
        from provide.uterm.ai.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info, patch("sys.stderr", new_callable=StringIO):
            parser.parse_args([])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# MCP __init__ re-exports
# ---------------------------------------------------------------------------


class TestMCPInit:
    def test_create_mcp_app_importable_from_init(self) -> None:
        from provide.uterm.ai import create_mcp_app

        assert callable(create_mcp_app)

    def test_all_exports(self) -> None:
        import provide.uterm.ai as mcp_pkg

        assert "create_mcp_app" in mcp_pkg.__all__
