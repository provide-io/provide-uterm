#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""MCP regression tests — hijack-related: multi-session isolation, snapshots,
events, key escapes, send guards, multi-read, lease expiry.

Split from test_mcp_regression.py.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import WorkerTermState
from provide.terminal.ai.server import create_mcp_app

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
# Multi-session isolation — two workers, independent hijacks
# ---------------------------------------------------------------------------


class TestMultiSessionIsolation:
    """Two workers hijacked simultaneously must not interfere."""

    async def test_concurrent_hijacks_independent(self) -> None:
        hub, app = _make_hub_app()
        _add_worker_with_snapshot(hub, "w1", "Screen-W1")
        _add_worker_with_snapshot(hub, "w2", "Screen-W2")
        mcp = _mcp_for(app)

        hid1 = await _acquire(mcp, "w1", owner="owner-a")
        hid2 = await _acquire(mcp, "w2", owner="owner-b")

        # Both succeeded with distinct hijack IDs
        assert hid1 != hid2

        # Send to w1 only
        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid1,
                "keys": "cmd1",
            },
        )
        assert d["success"] is True

        # Send to w2 only
        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w2",
                "hijack_id": hid2,
                "keys": "cmd2",
            },
        )
        assert d["success"] is True

        # Heartbeat both
        for wid, hid in [("w1", hid1), ("w2", hid2)]:
            d = await _call(
                mcp,
                "hijack_heartbeat",
                {
                    "worker_id": wid,
                    "hijack_id": hid,
                },
            )
            assert d["success"] is True

        # Release w1 — w2 should still be hijacked
        d = await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w1",
                "hijack_id": hid1,
            },
        )
        assert d["success"] is True

        # w2 still works
        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w2",
                "hijack_id": hid2,
                "keys": "more",
            },
        )
        assert d["success"] is True

        # Cleanup
        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w2",
                "hijack_id": hid2,
            },
        )

    async def test_cross_worker_hijack_id_rejected(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        _add_worker(hub, "w2")
        mcp = _mcp_for(app)

        hid1 = await _acquire(mcp, "w1")

        # Try using w1's hijack_id on w2
        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w2",
                "hijack_id": hid1,
                "keys": "x",
            },
        )
        assert d["success"] is False

        d = await _call(
            mcp,
            "hijack_heartbeat",
            {
                "worker_id": "w2",
                "hijack_id": hid1,
            },
        )
        assert d["success"] is False

        d = await _call(
            mcp,
            "hijack_step",
            {
                "worker_id": "w2",
                "hijack_id": hid1,
            },
        )
        assert d["success"] is False

        d = await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w2",
                "hijack_id": hid1,
            },
        )
        assert d["success"] is False

        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w1",
                "hijack_id": hid1,
            },
        )

    async def test_double_hijack_same_worker_rejected(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        hid = await _acquire(mcp, "w1", owner="first")
        d = await _call(
            mcp,
            "hijack_begin",
            {
                "worker_id": "w1",
                "owner": "second",
            },
        )
        assert d["success"] is False

        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w1",
                "hijack_id": hid,
            },
        )

    async def test_re_hijack_after_release(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        hid1 = await _acquire(mcp, "w1", owner="first")
        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w1",
                "hijack_id": hid1,
            },
        )

        hid2 = await _acquire(mcp, "w1", owner="second")
        assert hid2 != hid1
        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid2,
                "keys": "x",
            },
        )
        assert d["success"] is True
        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w1",
                "hijack_id": hid2,
            },
        )

    async def test_stale_hijack_id_rejected_after_release(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        hid = await _acquire(mcp, "w1")
        await _call(
            mcp,
            "hijack_release",
            {
                "worker_id": "w1",
                "hijack_id": hid,
            },
        )

        # Old hijack_id should fail
        d = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "keys": "x",
            },
        )
        assert d["success"] is False


# ---------------------------------------------------------------------------
# Snapshot read across output modes
# ---------------------------------------------------------------------------


class TestSnapshotOutputModes:
    """Verify all output modes process snapshots correctly."""

    async def test_text_mode_strips_ansi_no_metadata(self) -> None:
        hub, app = _make_hub_app()
        _add_worker_with_snapshot(hub, "w1", "\x1b[32mgreen\x1b[0m text")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "text",
                "wait_ms": 50,
            },
        )
        assert d["success"] is True
        snap = d["snapshot"]
        assert "\x1b" not in snap["screen"]
        assert "green text" in snap["screen"]
        assert "cursor" not in snap
        assert "cols" not in snap

    async def test_rendered_mode_strips_ansi_keeps_metadata(self) -> None:
        hub, app = _make_hub_app()
        _add_worker_with_snapshot(hub, "w1", "\x1b[31mred\x1b[0m")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "rendered",
                "wait_ms": 50,
            },
        )
        assert d["success"] is True
        snap = d["snapshot"]
        assert "\x1b" not in snap["screen"]
        assert snap["cols"] == 80
        assert snap["rows"] == 25

    async def test_raw_mode_preserves_ansi(self) -> None:
        hub, app = _make_hub_app()
        _add_worker_with_snapshot(hub, "w1", "\x1b[33myellow\x1b[0m")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d = await _call(
            mcp,
            "hijack_read",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "mode": "snapshot",
                "output": "raw",
                "wait_ms": 50,
            },
        )
        assert d["success"] is True
        assert "\x1b" in d["snapshot"]["screen"]


# ---------------------------------------------------------------------------
# Hijack with guard parameters
# ---------------------------------------------------------------------------


class TestHijackSendGuards:
    async def test_send_with_expect_prompt_id_timeout(self) -> None:
        """Guard waits for prompt, times out — send still succeeds (prompt not matched)."""
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
                "keys": "x",
                "expect_prompt_id": "nonexistent_prompt",
                "timeout_ms": 100,
            },
        )
        assert isinstance(d, dict)
        assert "success" in d

    async def test_send_with_expect_regex_timeout(self) -> None:
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
                "keys": "x",
                "expect_regex": r"impossible_pattern_xyz",
                "timeout_ms": 100,
            },
        )
        assert isinstance(d, dict)
        assert "success" in d


# ---------------------------------------------------------------------------
# Lease expiry semantics
# ---------------------------------------------------------------------------


class TestLeaseExpiry:
    async def test_heartbeat_extends_lease(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)
        hid = await _acquire(mcp, "w1")

        d1 = await _call(
            mcp,
            "hijack_heartbeat",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "lease_s": 60,
            },
        )
        exp1 = d1["lease_expires_at"]

        # Second heartbeat with longer lease
        d2 = await _call(
            mcp,
            "hijack_heartbeat",
            {
                "worker_id": "w1",
                "hijack_id": hid,
                "lease_s": 120,
            },
        )
        exp2 = d2["lease_expires_at"]
        assert exp2 > exp1

    async def test_custom_lease_on_acquire(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub, "w1")
        mcp = _mcp_for(app)

        d = await _call(
            mcp,
            "hijack_begin",
            {
                "worker_id": "w1",
                "lease_s": 30,
            },
        )
        assert d["success"] is True
        # Lease should expire ~30s from now (allow 5s tolerance)
        assert d["lease_expires_at"] < time.time() + 35
        assert d["lease_expires_at"] > time.time() + 25
