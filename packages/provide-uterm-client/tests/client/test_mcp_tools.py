#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the generic MCP tool functions."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport
from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import WorkerTermState

from provide.terminal.client.mcp_tools import hijack_tools

WID = "mcp-worker"


def _make_hub_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


def _add_worker(hub: TermHub, worker_id: str = WID) -> None:
    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    hub._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)


# ---------------------------------------------------------------------------
# Tool function metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_returns_six_tools(self) -> None:
        tools = hijack_tools("http://test")
        assert len(tools) == 6

    def test_tool_names(self) -> None:
        tools = hijack_tools("http://test")
        names = [fn.__name__ for fn in tools]
        assert names == [
            "hijack_begin",
            "hijack_heartbeat",
            "hijack_read",
            "hijack_send",
            "hijack_step",
            "hijack_release",
        ]

    def test_all_have_docstrings(self) -> None:
        tools = hijack_tools("http://test")
        for fn in tools:
            assert fn.__doc__, f"{fn.__name__} missing docstring"

    def test_all_are_coroutines(self) -> None:
        tools = hijack_tools("http://test")
        for fn in tools:
            assert inspect.iscoroutinefunction(fn), f"{fn.__name__} is not async"

    def test_signatures_have_type_hints(self) -> None:
        tools = hijack_tools("http://test")
        for fn in tools:
            sig = inspect.signature(fn)
            for name, param in sig.parameters.items():
                assert param.annotation != inspect.Parameter.empty, f"{fn.__name__}.{name} missing type hint"


# ---------------------------------------------------------------------------
# Full lifecycle via MCP tools
# ---------------------------------------------------------------------------


class TestMCPLifecycle:
    async def test_begin_send_read_step_release(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, heartbeat, read, send, step, release = tools

        # Begin
        result = await begin(WID, lease_s=60, owner="mcp-test")
        assert result["success"] is True
        hid = result["hijack_id"]

        # Send
        result = await send(WID, hid, keys="test\r")
        assert result["success"] is True

        # Read snapshot
        result = await read(WID, hid, mode="snapshot", wait_ms=50)
        assert result["success"] is True
        assert "snapshot" in result

        # Read events
        result = await read(WID, hid, mode="events")
        assert result["success"] is True
        assert "events" in result

        # Heartbeat
        result = await heartbeat(WID, hid, lease_s=120)
        assert result["success"] is True

        # Step
        result = await step(WID, hid)
        assert result["success"] is True

        # Release
        result = await release(WID, hid)
        assert result["success"] is True

    async def test_begin_no_worker(self) -> None:
        hub, app = _make_hub_app()
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin = tools[0]

        result = await begin(WID)
        assert result["success"] is False

    async def test_send_bad_hijack(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        send = tools[3]

        result = await send(WID, "bad-hijack-id", keys="x")
        assert result["success"] is False

    async def test_read_defaults_to_snapshot(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, read, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        # Default mode is "snapshot"
        result = await read(WID, hid, wait_ms=50)
        assert result["success"] is True
        assert "snapshot" in result


# ---------------------------------------------------------------------------
# Custom client kwargs forwarded
# ---------------------------------------------------------------------------


class TestOkHelper:
    def test_ok_with_non_dict_data(self) -> None:
        from provide.terminal.client.mcp_tools import _ok

        result = _ok(True, [1, 2, 3])
        assert result == {"success": True, "data": [1, 2, 3]}

    def test_ok_with_dict_data(self) -> None:
        from provide.terminal.client.mcp_tools import _ok

        result = _ok(False, {"error": "fail"})
        assert result == {"success": False, "error": "fail"}


class TestClientKwargs:
    async def test_entity_prefix_forwarded(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        # Wrong prefix → 404
        tools = hijack_tools("http://test", transport=transport, entity_prefix="/bot")
        begin = tools[0]

        result = await begin(WID)
        assert result["success"] is False
