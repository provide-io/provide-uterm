#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack lifecycle, error paths, send guards, and read output mode tests."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

from provide.uterm.ai.server import create_mcp_app

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
    hub.registry._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


def _mcp_for(app: FastAPI, **kwargs: object) -> MCPServer:
    """Return an MCP app backed by ASGI transport to *app*.

    These tests exercise hijack/worker-control tools that require admin role,
    so the test factory explicitly opts in via ``default_role="admin"``.  The
    Finding #2 fix lowered the unspecified default to ``operator``.
    """
    kwargs.setdefault("default_role", "admin")
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


async def _call(mcp: MCPServer, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an MCP tool and return the structured_content dict."""
    result = await mcp.call_tool(tool, args or {})
    # MCPServer.call_tool() returns CallToolResult | InputRequiredResult; only
    # the former carries structured_content. None of these tests exercise the
    # elicitation path that produces InputRequiredResult, so a plain isinstance
    # narrows the union for mypy without changing runtime behavior.
    assert isinstance(result, CallToolResult)
    return result.structured_content  # type: ignore[no-any-return]


async def _acquire(mcp: MCPServer, worker_id: str = WID, **kw: Any) -> str:
    """Acquire hijack and return hijack_id."""
    data = await _call(mcp, "hijack_begin", {"worker_id": worker_id, **kw})
    assert data["success"] is True
    return data["hijack_id"]


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
        hub.registry._workers[WID].hijack_session = HijackSession(
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


# (TestHijackSendGuards, TestHijackReadOutputModes moved to test_mcp_hijack_2.py)
