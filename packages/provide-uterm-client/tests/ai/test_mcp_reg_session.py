#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""MCP regression tests — session lifecycle, mode switching, worker control.

Split from test_mcp_regression.py.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport
from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import WorkerTermState

from provide.uterm.ai.server import create_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH_HEADERS = {"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"}


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


def _add_worker_with_snapshot(
    hub: TermHub,
    worker_id: str,
    screen: str,
) -> AsyncMock:
    ws = _add_worker(hub, worker_id)
    hub._workers[worker_id].last_snapshot = {
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": f"hash-{worker_id}",
        "ts": time.time() + 100,
    }
    return ws


def _mcp_for(app: FastAPI) -> FastMCP:
    return create_mcp_app("http://test", transport=ASGITransport(app=app))


def _server_app(
    sessions: list[dict[str, Any]] | None = None,
) -> FastAPI:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import config_from_mapping

    if sessions is None:
        sessions = [
            {
                "session_id": "sh-1",
                "display_name": "Shell One",
                "connector_type": "shell",
                "auto_start": False,
            },
            {
                "session_id": "sh-2",
                "display_name": "Shell Two",
                "connector_type": "shell",
                "auto_start": False,
            },
        ]
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": sessions,
        }
    )
    return create_server_app(cfg)


def _mcp_for_server(app: FastAPI) -> FastMCP:
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        headers=_AUTH_HEADERS,
    )


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
# Mode switching
# ---------------------------------------------------------------------------


class TestModeSwitching:
    """Session and worker mode switching through MCP tools."""

    async def test_session_mode_round_trip(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        d = await _call(
            mcp,
            "session_set_mode",
            {
                "session_id": "sh-1",
                "mode": "hijack",
            },
        )
        assert d["success"] is True
        assert d["input_mode"] == "hijack"

        d = await _call(
            mcp,
            "session_set_mode",
            {
                "session_id": "sh-1",
                "mode": "open",
            },
        )
        assert d["success"] is True
        assert d["input_mode"] == "open"

    async def test_worker_mode_round_trip(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        d = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": "w1",
                "mode": "open",
            },
        )
        assert d["success"] is True
        assert d["input_mode"] == "open"

        d = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": "w1",
                "mode": "hijack",
            },
        )
        assert d["success"] is True
        assert d["input_mode"] == "hijack"

    async def test_mode_persists_across_reads(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        await _call(
            mcp,
            "session_set_mode",
            {
                "session_id": "sh-1",
                "mode": "hijack",
            },
        )
        d = await _call(mcp, "session_status", {"session_id": "sh-1"})
        assert d["input_mode"] == "hijack"

        await _call(
            mcp,
            "session_set_mode",
            {
                "session_id": "sh-1",
                "mode": "open",
            },
        )
        d = await _call(mcp, "session_status", {"session_id": "sh-1"})
        assert d["input_mode"] == "open"


# ---------------------------------------------------------------------------
# Multi-session server lifecycle
# ---------------------------------------------------------------------------


class TestMultiSessionServerLifecycle:
    """Multiple sessions through the full server app MCP layer."""

    async def test_list_shows_all_configured(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        d = await _call(mcp, "session_list")
        assert d["success"] is True
        ids = {s["session_id"] for s in d["data"]}
        assert {"sh-1", "sh-2"} <= ids

    async def test_connect_disconnect_independent(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        # Connect sh-1 only
        d = await _call(mcp, "session_connect", {"session_id": "sh-1"})
        assert d["success"] is True

        # sh-2 still stopped
        d2 = await _call(mcp, "session_status", {"session_id": "sh-2"})
        assert d2["lifecycle_state"] == "stopped"

        await _call(mcp, "session_disconnect", {"session_id": "sh-1"})

    async def test_ephemeral_session_appears_in_list(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        d = await _call(
            mcp,
            "session_create",
            {
                "connector_type": "shell",
                "display_name": "Ephemeral-Test",
            },
        )
        assert d["success"] is True
        eph_id = d["session_id"]

        listing = await _call(mcp, "session_list")
        ids = {s["session_id"] for s in listing["data"]}
        assert eph_id in ids

    async def test_ephemeral_session_properties(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        d = await _call(
            mcp,
            "session_create",
            {
                "connector_type": "shell",
                "display_name": "PropTest",
                "input_mode": "hijack",
            },
        )
        assert d["success"] is True
        assert d["visibility"] == "private"
        assert d["owner"] == "tester"

    async def test_session_status_unknown_returns_failure(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        d = await _call(mcp, "session_status", {"session_id": "no-such"})
        assert d["success"] is False

    async def test_connect_disconnect_unknown_returns_failure(self) -> None:
        app = _server_app()
        mcp = _mcp_for_server(app)

        for tool in ("session_connect", "session_disconnect"):
            d = await _call(mcp, tool, {"session_id": "no-such"})
            assert d["success"] is False


# ---------------------------------------------------------------------------
# Worker control on disconnected/missing worker
# ---------------------------------------------------------------------------


class TestWorkerControlErrors:
    async def test_input_mode_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)

        d = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": "ghost",
                "mode": "open",
            },
        )
        assert d["success"] is False

    async def test_disconnect_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)

        d = await _call(mcp, "worker_disconnect", {"worker_id": "ghost"})
        assert d["success"] is False

    async def test_hijack_begin_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)

        d = await _call(mcp, "hijack_begin", {"worker_id": "ghost"})
        assert d["success"] is False

    async def test_worker_disconnect_then_hijack_fails(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        d = await _call(mcp, "worker_disconnect", {"worker_id": "w1"})
        assert d["success"] is True

        d = await _call(mcp, "hijack_begin", {"worker_id": "w1"})
        assert d["success"] is False


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class TestEventLog:
    async def test_events_track_hijack_lifecycle(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        hid = await _acquire(mcp, "w1")
        await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "test",
            },
        )
        await _call(
            mcp,
            "hijack_heartbeat",
            {
                "worker_id": "w1",
                "hijack_id": hid,
            },
        )

        d = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "mode": "events",
            },
        )
        assert d["success"] is True
        events = d["events"]
        types = [e["type"] for e in events]
        assert "hijack_acquired" in types
        assert "hijack_send" in types
        assert "hijack_heartbeat" in types

        # Sequence numbers are monotonically increasing
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))

    async def test_events_after_seq_filters(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        hid = await _acquire(mcp, "w1")
        await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "a",
            },
        )
        await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "b",
            },
        )

        # Get all events to find a midpoint seq
        d = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "mode": "events",
            },
        )
        all_events = d["events"]
        mid_seq = all_events[len(all_events) // 2]["seq"]

        # Request only events after mid_seq
        d2 = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "mode": "events",
                "after_seq": mid_seq,
            },
        )
        filtered = d2["events"]
        assert all(e["seq"] > mid_seq for e in filtered)
        assert len(filtered) < len(all_events)


# ---------------------------------------------------------------------------
# Session read across multiple sessions
# ---------------------------------------------------------------------------


class TestSessionReadMultiple:
    async def test_read_two_sessions_independently(self) -> None:
        hub, app = _make_hub_app()
        _add_worker_with_snapshot(hub, "w1", "Screen-Alpha")
        _add_worker_with_snapshot(hub, "w2", "Screen-Beta")
        mcp = _mcp_for(app)

        # Read via hijack — each shows its own screen
        hid1 = await _acquire(mcp, "w1")
        hid2 = await _acquire(mcp, "w2")

        d1 = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid1,
                "mode": "snapshot",
                "output": "text",
                "wait_ms": 50,
            },
        )
        d2 = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w2",
                "hijack_id": hid2,
                "mode": "snapshot",
                "output": "text",
                "wait_ms": 50,
            },
        )

        assert "Alpha" in d1["snapshot"]["screen"]
        assert "Beta" in d2["snapshot"]["screen"]
        assert "Beta" not in d1["snapshot"]["screen"]
        assert "Alpha" not in d2["snapshot"]["screen"]
