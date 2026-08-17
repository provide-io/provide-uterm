#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage for GUI MCP tools (success path + invalid-id rejection branches)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

from provide.uterm.ai.server import create_mcp_app

WID = "gui-worker"
_BAD = "../evil"


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


def _mcp_for(app: FastAPI) -> MCPServer:
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        default_role="admin",
    )


async def _call(mcp: MCPServer, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(tool, args or {})
    # MCPServer.call_tool() returns CallToolResult | InputRequiredResult; only
    # the former carries structured_content. None of these tests exercise the
    # elicitation path that produces InputRequiredResult, so a plain isinstance
    # narrows the union for mypy without changing runtime behavior.
    assert isinstance(result, CallToolResult)
    return result.structured_content  # type: ignore[no-any-return]


class TestGuiTools:
    async def test_gui_lifecycle_and_input(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)

        begin = await _call(mcp, "gui_hijack_begin", {"worker_id": WID})
        assert begin.get("success") is True
        hid = begin["hijack_id"]

        # Screenshot / input paths (may fail downstream if no GUI session; still
        # exercise the tool registration + id validation success branch).
        for tool, args in (
            ("gui_screenshot", {"worker_id": WID, "hijack_id": hid}),
            ("gui_click", {"worker_id": WID, "hijack_id": hid, "x": 1, "y": 2}),
            ("gui_type", {"worker_id": WID, "hijack_id": hid, "text": "hi"}),
            ("gui_key", {"worker_id": WID, "hijack_id": hid, "key_name": "Enter"}),
            (
                "gui_drag",
                {
                    "worker_id": WID,
                    "hijack_id": hid,
                    "start_x": 0,
                    "start_y": 0,
                    "end_x": 5,
                    "end_y": 5,
                },
            ),
        ):
            data = await _call(mcp, tool, args)
            assert isinstance(data, dict)

        rel = await _call(mcp, "gui_hijack_release", {"worker_id": WID, "hijack_id": hid})
        assert isinstance(rel, dict)

    async def test_gui_tools_reject_bad_ids(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)

        bad_begin = await _call(mcp, "gui_hijack_begin", {"worker_id": _BAD})
        assert bad_begin.get("success") is False

        cases: list[tuple[str, dict[str, Any]]] = [
            ("gui_hijack_release", {"worker_id": _BAD, "hijack_id": "h1"}),
            ("gui_hijack_release", {"worker_id": WID, "hijack_id": _BAD}),
            ("gui_screenshot", {"worker_id": _BAD, "hijack_id": "h1"}),
            ("gui_click", {"worker_id": _BAD, "hijack_id": "h1", "x": 0, "y": 0}),
            ("gui_type", {"worker_id": _BAD, "hijack_id": "h1", "text": "x"}),
            ("gui_key", {"worker_id": _BAD, "hijack_id": "h1", "key_name": "Enter"}),
            (
                "gui_drag",
                {
                    "worker_id": _BAD,
                    "hijack_id": "h1",
                    "start_x": 0,
                    "start_y": 0,
                    "end_x": 1,
                    "end_y": 1,
                },
            ),
        ]
        for tool, args in cases:
            data = await _call(mcp, tool, args)
            assert data.get("success") is False, tool
