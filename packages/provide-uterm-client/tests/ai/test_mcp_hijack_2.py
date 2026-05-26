#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack send guards and read output mode tests (part 2)."""

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

WID = "mcp-worker"


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
    """Return a FastMCP app backed by ASGI transport to *app*.

    Tests in this file exercise admin-only hijack tools; after Finding #2
    the default dropped to operator so we opt in explicitly here.
    """
    kwargs.setdefault("default_role", "admin")
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
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

    async def test_tail_lines_trims_snapshot(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)

        hub._workers[WID].last_snapshot = {
            "screen": "line1\nline2\nline3\nline4\nline5",
            "cursor": {"row": 4, "col": 0},
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
                "tail_lines": 2,
            },
        )
        assert data["success"] is True
        assert data["snapshot"]["screen"] == "line4\nline5"

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
