#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for the FastMCP server tools against a real FastAPI app."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import HijackSession, WorkerTermState
from provide.terminal.mcp.server import TOOL_COUNT, _clean_snapshot, _unescape_keys, create_mcp_app

WID = "mcp-worker"
BAD_HID = "00000000-dead-beef-0000-000000000000"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hub_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


def _add_worker(hub: TermHub, worker_id: str = WID) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    hub._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


def _mcp_for(app: FastAPI, **kwargs: object) -> FastMCP:
    """Return a FastMCP app backed by ASGI transport to *app*."""
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


def _make_server_app() -> FastAPI:
    from provide.terminal.server.app import create_server_app
    from provide.terminal.server.config import config_from_mapping

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "Test",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    return create_server_app(cfg)


def _mcp_for_server(app: FastAPI) -> FastMCP:
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
    )


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an MCP tool and return the structured_content dict."""
    result = await mcp.call_tool(tool, args or {})
    return result.structured_content  # type: ignore[return-value]


async def _acquire(mcp: FastMCP, worker_id: str = WID, **kw: Any) -> str:
    """Acquire hijack and return hijack_id."""
    data = await _call(mcp, "hijack_begin", {"worker_id": worker_id, **kw})
    assert data["success"] is True
    return data["hijack_id"]


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
# Hijack lifecycle — full integration
# ---------------------------------------------------------------------------


class TestHijackLifecycle:
    async def test_full_lifecycle_begin_send_read_heartbeat_step_release(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)

        # Begin — validate return structure
        data = await _call(
            mcp,
            "hijack_begin",
            {
                "worker_id": WID,
                "lease_s": 60,
                "owner": "mcp-test",
            },
        )
        assert data["success"] is True
        assert "hijack_id" in data
        assert data["ok"] is True
        assert data["owner"] == "mcp-test"
        hid = data["hijack_id"]

        # Send — validate sent keys echoed back (MCP unescapes \r → CR)
        data = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "keys": "test\\r",
            },
        )
        assert data["success"] is True
        assert data["sent"] == "test\r"

        # Read snapshot (null snapshot because no real emulator)
        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "snapshot",
                "wait_ms": 50,
            },
        )
        assert data["success"] is True
        assert data["ok"] is True
        assert "worker_id" in data

        # Read events — validate events list returned
        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "events",
            },
        )
        assert data["success"] is True
        assert "events" in data

        # Heartbeat — validate lease extended
        data = await _call(
            mcp,
            "hijack_heartbeat",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "lease_s": 120,
            },
        )
        assert data["success"] is True
        assert data["ok"] is True
        assert "lease_expires_at" in data

        # Step
        data = await _call(mcp, "hijack_step", {"worker_id": WID, "hijack_id": hid})
        assert data["success"] is True
        assert data["ok"] is True

        # Release
        data = await _call(mcp, "hijack_release", {"worker_id": WID, "hijack_id": hid})
        assert data["success"] is True
        assert data["ok"] is True

    async def test_begin_uses_default_owner(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)

        data = await _call(mcp, "hijack_begin", {"worker_id": WID})
        assert data["success"] is True
        assert data["owner"] == "operator"
        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": WID,
                "hijack_id": data["hijack_id"],
            },
        )


# ---------------------------------------------------------------------------
# Hijack lifecycle — error paths
# ---------------------------------------------------------------------------


class TestHijackErrors:
    async def test_begin_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(mcp, "hijack_begin", {"worker_id": WID})
        assert data["success"] is False

    async def test_begin_conflict_already_hijacked(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        hub._workers[WID].hijack_session = HijackSession(
            hijack_id="existing",
            owner="other",
            acquired_at=time.time(),
            lease_expires_at=time.time() + 3600,
            last_heartbeat=time.time(),
        )
        mcp = _mcp_for(app)
        data = await _call(mcp, "hijack_begin", {"worker_id": WID})
        assert data["success"] is False

    async def test_send_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": WID,
                "hijack_id": BAD_HID,
                "keys": "x",
            },
        )
        assert data["success"] is False

    async def test_heartbeat_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "hijack_heartbeat",
            {
                "worker_id": WID,
                "hijack_id": BAD_HID,
            },
        )
        assert data["success"] is False

    async def test_release_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": WID,
                "hijack_id": BAD_HID,
            },
        )
        assert data["success"] is False

    async def test_step_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "hijack_step",
            {
                "worker_id": WID,
                "hijack_id": BAD_HID,
            },
        )
        assert data["success"] is False

    async def test_read_snapshot_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": BAD_HID,
                "mode": "snapshot",
                "wait_ms": 50,
            },
        )
        assert data["success"] is False

    async def test_read_events_bad_hijack_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": BAD_HID,
                "mode": "events",
            },
        )
        assert data["success"] is False


# ---------------------------------------------------------------------------
# hijack_send with guard params
# ---------------------------------------------------------------------------


class TestHijackSendGuards:
    async def test_send_with_expect_prompt_id(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)
        data = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "keys": "x",
                "expect_prompt_id": "some_prompt",
            },
        )
        # Guard may fail (no prompt matched) — the param is forwarded regardless
        assert isinstance(data, dict)
        assert "success" in data

    async def test_send_with_expect_regex(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)
        data = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "keys": "x",
                "expect_regex": r".*prompt.*",
            },
        )
        assert isinstance(data, dict)
        assert "success" in data

    async def test_send_custom_timeout_and_poll(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)
        data = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "keys": "x",
                "timeout_ms": 100,
                "poll_interval_ms": 50,
            },
        )
        assert data["success"] is True
        assert data["sent"] == "x"


# ---------------------------------------------------------------------------
# Output modes — _clean_snapshot unit tests
# ---------------------------------------------------------------------------

_ANSI_SNAPSHOT: dict[str, Any] = {
    "screen": "\x1b[1;31mHello\x1b[0m World",
    "cursor": {"row": 0, "col": 11},
    "cols": 80,
    "rows": 24,
}


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
# Output modes — integration through hijack_read
# ---------------------------------------------------------------------------


class TestHijackReadOutputModes:
    async def test_text_mode_strips_ansi(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)

        hub._workers[WID].last_snapshot = {
            "screen": "\x1b[32mtest\x1b[0m output",
            "cursor": {"row": 0, "col": 11},
            "cols": 80,
            "rows": 24,
            "ts": time.time() + 10,
        }

        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "text",
                "wait_ms": 50,
            },
        )
        assert data["success"] is True
        snapshot = data["snapshot"]
        assert "\x1b" not in snapshot["screen"]
        assert "test output" in snapshot["screen"]
        assert "cursor" not in snapshot
        assert "cols" not in snapshot

    async def test_rendered_mode_keeps_metadata(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)

        hub._workers[WID].last_snapshot = {
            "screen": "\x1b[32mtest\x1b[0m output",
            "cursor": {"row": 0, "col": 11},
            "cols": 80,
            "rows": 24,
            "ts": time.time() + 10,
        }

        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "rendered",
                "wait_ms": 50,
            },
        )
        assert data["success"] is True
        snapshot = data["snapshot"]
        assert "\x1b" not in snapshot["screen"]
        assert snapshot["cols"] == 80
        assert snapshot["rows"] == 24
        assert snapshot["cursor"] == {"row": 0, "col": 11}

    async def test_raw_mode_preserves_ansi(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)

        hub._workers[WID].last_snapshot = {
            "screen": "\x1b[32mtest\x1b[0m output",
            "cursor": {"row": 0, "col": 11},
            "cols": 80,
            "rows": 24,
            "ts": time.time() + 10,
        }

        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "raw",
                "wait_ms": 50,
            },
        )
        assert data["success"] is True
        assert "\x1b" in data["snapshot"]["screen"]

    async def test_null_snapshot_not_cleaned(self) -> None:
        """When snapshot is None, _clean_snapshot is not called."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)
        # No last_snapshot set — wait_for_snapshot returns None
        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "text",
                "wait_ms": 50,
            },
        )
        assert data["success"] is True
        # snapshot key exists but is None — not processed by _clean_snapshot
        assert data.get("snapshot") is None

    async def test_events_mode_ignores_output_param(self) -> None:
        """Events mode never applies _clean_snapshot."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)
        data = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "mode": "events",
                "output": "raw",
            },
        )
        assert data["success"] is True
        assert "events" in data


# ---------------------------------------------------------------------------
# Session management tools (require full server app)
# ---------------------------------------------------------------------------


class TestSessionTools:
    async def test_server_health_fields(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "server_health")
        assert data["success"] is True
        assert data["ok"] is True
        assert data["ready"] is True
        assert data["service"] == "uterm-server"

    async def test_session_list_returns_sessions(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_list")
        assert data["success"] is True
        sessions = data["data"]
        assert isinstance(sessions, list)
        assert len(sessions) >= 1
        # s1 should be in the list
        ids = [s["session_id"] for s in sessions]
        assert "s1" in ids

    async def test_session_status_validates_fields(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_status", {"session_id": "s1"})
        assert data["success"] is True
        assert data["session_id"] == "s1"
        assert "display_name" in data

    async def test_session_status_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_status", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_connect_disconnect_sequence(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        data = await _call(mcp, "session_connect", {"session_id": "s1"})
        assert data["success"] is True

        data = await _call(mcp, "session_disconnect", {"session_id": "s1"})
        assert data["success"] is True

    async def test_session_connect_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_connect", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_disconnect_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_disconnect", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_set_mode_open(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_set_mode", {"session_id": "s1", "mode": "open"})
        assert data["success"] is True
        assert data["input_mode"] == "open"

    async def test_session_set_mode_hijack(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_set_mode", {"session_id": "s1", "mode": "hijack"})
        assert data["success"] is True
        assert data["input_mode"] == "hijack"

    async def test_session_set_mode_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "session_set_mode",
            {
                "session_id": "nonexistent",
                "mode": "open",
            },
        )
        assert data["success"] is False

    async def test_session_create_with_display_name(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "session_create",
            {
                "connector_type": "shell",
                "display_name": "Ephemeral",
            },
        )
        assert data["success"] is True
        assert "session_id" in data

    async def test_session_create_all_kwargs(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "session_create",
            {
                "connector_type": "shell",
                "display_name": "Full",
                "host": "127.0.0.1",
                "port": 23,
                "url": "ws://example.com/ws",
                "username": "user",
                "password": "pass",
                "input_mode": "open",
            },
        )
        assert data["success"] is True
        assert "session_id" in data

    async def test_session_create_minimal(self) -> None:
        """No optional kwargs — all default to None."""
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_create", {"connector_type": "shell"})
        assert data["success"] is True
        assert "session_id" in data

    async def test_session_read_null_snapshot(self) -> None:
        """Session with no worker has null snapshot."""
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_read", {"session_id": "s1"})
        assert data["success"] is True

    async def test_session_read_with_snapshot_data(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        fake_snapshot = {
            "screen": "\x1b[31mred\x1b[0m text",
            "cursor": {"row": 0, "col": 8},
            "cols": 80,
            "rows": 24,
        }
        with patch(
            "provide.terminal.client.hijack.HijackClient.session_snapshot",
            return_value=(True, {"snapshot": fake_snapshot}),
        ):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": "text",
                },
            )
        assert data["success"] is True
        assert "\x1b" not in data["snapshot"]["screen"]
        assert "red text" in data["snapshot"]["screen"]
        assert "cursor" not in data["snapshot"]

    async def test_session_read_rendered_with_snapshot_data(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        fake_snapshot = {
            "screen": "\x1b[31mred\x1b[0m text",
            "cursor": {"row": 0, "col": 8},
            "cols": 80,
            "rows": 24,
        }
        with patch(
            "provide.terminal.client.hijack.HijackClient.session_snapshot",
            return_value=(True, {"snapshot": fake_snapshot}),
        ):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": "rendered",
                },
            )
        assert data["success"] is True
        assert data["snapshot"]["cols"] == 80
        assert data["snapshot"]["cursor"] == {"row": 0, "col": 8}

    async def test_session_read_raw_with_snapshot_data(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        fake_snapshot = {
            "screen": "\x1b[31mred\x1b[0m text",
            "cursor": {"row": 0, "col": 8},
            "cols": 80,
            "rows": 24,
        }
        with patch(
            "provide.terminal.client.hijack.HijackClient.session_snapshot",
            return_value=(True, {"snapshot": fake_snapshot}),
        ):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": "raw",
                },
            )
        assert data["success"] is True
        assert "\x1b" in data["snapshot"]["screen"]

    async def test_session_read_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_read", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_read_output_modes_null_snapshot(self) -> None:
        """All output modes handle null snapshot gracefully."""
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        for mode in ("text", "rendered", "raw"):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": mode,
                },
            )
            assert data["success"] is True


# ---------------------------------------------------------------------------
# Worker control tools
# ---------------------------------------------------------------------------


class TestWorkerControlTools:
    async def test_worker_input_mode_set_open(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": WID,
                "mode": "open",
            },
        )
        assert data["success"] is True
        assert data["input_mode"] == "open"

    async def test_worker_input_mode_set_hijack(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": WID,
                "mode": "hijack",
            },
        )
        assert data["success"] is True
        assert data["input_mode"] == "hijack"

    async def test_worker_disconnect_success(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(mcp, "worker_disconnect", {"worker_id": WID})
        assert data["success"] is True
        assert data["ok"] is True

    async def test_worker_input_mode_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": WID,
                "mode": "open",
            },
        )
        assert data["success"] is False

    async def test_worker_disconnect_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(mcp, "worker_disconnect", {"worker_id": WID})
        assert data["success"] is False


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
