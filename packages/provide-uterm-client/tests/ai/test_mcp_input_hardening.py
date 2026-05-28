#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Input-hardening tests for the MCP tool surface (Lane A2).

MCP-send: ``hijack_send`` must sanitize keystrokes *after* unescaping so an
LLM cannot drive a hijacked terminal with arbitrary ANSI/OSC/NUL bytes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

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
    kwargs.setdefault("default_role", "admin")
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(tool, args or {})
    return result.structured_content  # type: ignore[return-value]


async def _acquire(mcp: FastMCP, worker_id: str = WID, **kw: Any) -> str:
    data = await _call(mcp, "hijack_begin", {"worker_id": worker_id, **kw})
    assert data["success"] is True
    return data["hijack_id"]


# ---------------------------------------------------------------------------
# MCP-send: sanitize hijack_send after unescaping
# ---------------------------------------------------------------------------


class TestHijackSendSanitization:
    async def test_strips_injected_control_sequences(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        hid = await _acquire(mcp)

        # OSC sequence (\x1b]0;pwned\x07) and a NUL, supplied as escape text.
        data = await _call(
            mcp,
            "hijack_send",
            {
                "worker_id": WID,
                "hijack_id": hid,
                "keys": "ls\\x00\\x1b]0;pwned\\x07rm -rf /",
            },
        )
        assert data["success"] is True
        wire = data["sent"]
        # NUL and BEL (the OSC string terminator) are stripped, so the embedded
        # OSC sequence can never terminate and drive terminal title/clipboard.
        # ESC itself stays (it is an intentionally-allowed control byte), but
        # without a terminator the payload is inert.
        assert "\x00" not in wire  # NUL filtered
        assert "\x07" not in wire  # BEL (OSC terminator) filtered
        assert "\x9c" not in wire  # ST (8-bit OSC terminator) filtered
        assert len(wire.encode("utf-8")) <= 4096

    async def test_esc_and_ctrl_c_still_allowed(self) -> None:
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
                "keys": "\\x03\\e",
            },
        )
        assert data["success"] is True
        assert data["sent"] == "\x03\x1b"

    async def test_caps_oversized_keystrokes(self) -> None:
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
                "keys": "A" * 9000,
            },
        )
        assert data["success"] is True
        assert len(data["sent"].encode("utf-8")) <= 4096
