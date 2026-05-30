#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Input-hardening tests for the MCP tool surface (Lane A2).

* MCP-send: ``hijack_send`` must sanitize keystrokes *after* unescaping so an
  LLM cannot drive a hijacked terminal with arbitrary ANSI/OSC/NUL bytes.
* MCP-host: ``session_create`` must reject internal / metadata targets.
* MCP-redos: user-supplied match patterns must be length-bounded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
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


# ---------------------------------------------------------------------------
# MCP-host: validate session_create host
# ---------------------------------------------------------------------------


class TestSessionCreateHostValidation:
    @pytest.mark.parametrize(
        "host",
        [
            "169.254.169.254",
            "127.0.0.1",
            "localhost",
            "10.0.0.5",
            "::1",
            "metadata.google.internal",
            "fe80::1",
            "192.168.1.1",
            "172.16.0.1",
            "[::1]",
            "0.0.0.0",
        ],
    )
    def test_validate_rejects_internal_hosts(self, host: str) -> None:
        from provide.uterm.ai.server_impl import _validate_session_create_config

        rejection = _validate_session_create_config(
            connector_type="telnet",
            url=None,
            port=23,
            host=host,
        )
        assert rejection is not None
        assert rejection["error"] == "invalid_host"

    def test_validate_allows_public_host(self) -> None:
        from provide.uterm.ai.server_impl import _validate_session_create_config

        assert (
            _validate_session_create_config(
                connector_type="telnet",
                url=None,
                port=23,
                host="example.com",
            )
            is None
        )

    def test_validate_allows_public_ip(self) -> None:
        from provide.uterm.ai.server_impl import _validate_session_create_config

        assert (
            _validate_session_create_config(
                connector_type="telnet",
                url=None,
                port=23,
                host="93.184.216.34",
            )
            is None
        )

    def test_validate_allows_missing_host(self) -> None:
        from provide.uterm.ai.server_impl import _validate_session_create_config

        assert (
            _validate_session_create_config(
                connector_type="shell",
                url=None,
                port=None,
                host=None,
            )
            is None
        )

    async def test_session_create_rejects_internal_host_over_mcp(self) -> None:
        hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "session_create",
            {"connector_type": "telnet", "host": "169.254.169.254", "port": 23},
        )
        assert data["success"] is False
        assert data["error"] == "invalid_host"


# ---------------------------------------------------------------------------
# MCP-host: validate session_create host INSIDE url (SSRF via url argument)
# ---------------------------------------------------------------------------


class TestSessionCreateUrlHostValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "ws://169.254.169.254/term",
            "ws://127.0.0.1:8080",
            "wss://10.0.0.5/terminal",
            "ws://[::1]:9000/x",
            "ws://localhost:8080/term",
            "wss://metadata.google.internal/term",
            "ws://192.168.1.1/term",
        ],
    )
    def test_validate_rejects_internal_host_in_url(self, url: str) -> None:
        from provide.uterm.ai.server_impl import _validate_session_create_config

        rejection = _validate_session_create_config(connector_type="websocket", url=url, port=None)
        assert rejection is not None
        assert rejection["error"] == "invalid_host"

    def test_validate_allows_external_host_in_url(self) -> None:
        from provide.uterm.ai.server_impl import _validate_session_create_config

        result = _validate_session_create_config(
            connector_type="websocket", url="wss://example.com:443/term", port=None
        )
        assert result is None or result.get("error") != "invalid_host"

    async def test_session_create_rejects_internal_host_in_url_over_mcp(self) -> None:
        hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "session_create",
            {"connector_type": "websocket", "url": "ws://169.254.169.254/term"},
        )
        assert data["success"] is False
        assert data["error"] == "invalid_host"


# ---------------------------------------------------------------------------
# MCP-redos: bound user-supplied regex
# ---------------------------------------------------------------------------


class TestUserPatternBounds:
    def test_compile_user_pattern_caps_length(self) -> None:
        from provide.uterm.ai.server_impl import _compile_user_pattern

        with pytest.raises(ValueError, match="pattern"):
            _compile_user_pattern("x" * 2000)

    def test_compile_user_pattern_rejects_invalid(self) -> None:
        from provide.uterm.ai.server_impl import _compile_user_pattern

        with pytest.raises(ValueError, match="pattern"):
            _compile_user_pattern("(")

    def test_compile_user_pattern_accepts_normal(self) -> None:
        from provide.uterm.ai.server_impl import _compile_user_pattern

        compiled = _compile_user_pattern("foo.*bar")
        assert compiled.search("xfooXbary") is not None

    async def test_session_subscribe_rejects_oversized_pattern(self) -> None:
        hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "session_subscribe",
            {"session_id": "s1", "pattern": "a" * 2000},
        )
        assert data["success"] is False
        assert data["error"] == "invalid_pattern"

    async def test_session_watch_rejects_oversized_pattern(self) -> None:
        hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "session_watch",
            {"session_id": "s1", "pattern": "a" * 2000},
        )
        assert data["success"] is False
        assert data["error"] == "invalid_pattern"

    async def test_hijack_send_rejects_oversized_expect_regex(self) -> None:
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
                "keys": "ls",
                "expect_regex": "a" * 2000,
            },
        )
        assert data["success"] is False
        assert data["error"] == "invalid_pattern"
