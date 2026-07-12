#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the session_watch MCP tool and the /events/watch REST endpoint."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport
from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.models import RecordingConfig
from provide.uterm.server.registry import SessionRegistry

from provide.uterm.ai.server import TOOL_COUNT, create_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server_app_with_bus() -> tuple[FastAPI, TermHub, EventBus]:
    """Create a minimal server app with an EventBus wired into TermHub."""
    from provide.uterm.server.app import create_server_app

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
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
    app = create_server_app(cfg)
    # Inject EventBus into the hub after app construction (via registry)
    # We patch the hub's _event_bus directly since the app sets it up in lifespan.
    bus = EventBus()
    # We return app + bus; tests wire them together after startup.
    return app, bus


def _mcp_for_server(app: FastAPI) -> FastMCP:
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
    )


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(tool, args or {})
    return result.structured_content  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# TOOL_COUNT sanity check
# ---------------------------------------------------------------------------


def test_tool_count_is_28() -> None:
    assert TOOL_COUNT == 28


# ---------------------------------------------------------------------------
# session_watch MCP tool — mock client
# ---------------------------------------------------------------------------


class TestSessionWatchMcpTool:
    async def test_watch_returns_events(self) -> None:
        from provide.uterm.server.app import create_server_app

        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {
                    "mode": "header",
                    "header_mode_acknowledged": True,
                    "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                },
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
        app = create_server_app(cfg)
        mcp = _mcp_for_server(app)

        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "events": [{"worker_id": "s1", "seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}],
                        "dropped_count": 0,
                        "timed_out": True,
                    },
                )
            ),
        ):
            data = await _call(mcp, "session_watch", {"session_id": "s1"})

        assert data["success"] is True
        assert "events" in data
        assert data["dropped_count"] == 0
        assert data["timed_out"] is True

    async def test_watch_client_error_returns_failure(self) -> None:
        from provide.uterm.server.app import create_server_app

        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {
                    "mode": "header",
                    "header_mode_acknowledged": True,
                    "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                },
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
        app = create_server_app(cfg)
        mcp = _mcp_for_server(app)

        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(return_value=(False, {"error": "connection refused"})),
        ):
            data = await _call(mcp, "session_watch", {"session_id": "s1"})

        assert data["success"] is False

    async def test_watch_passes_event_types_and_pattern(self) -> None:
        from provide.uterm.server.app import create_server_app

        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {
                    "mode": "header",
                    "header_mode_acknowledged": True,
                    "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                },
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
        app = create_server_app(cfg)
        mcp = _mcp_for_server(app)

        mock_watch = AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True}))
        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=mock_watch,
        ):
            await _call(
                mcp,
                "session_watch",
                {
                    "session_id": "s1",
                    "event_types": "snapshot,input_send",
                    "pattern": r"\$",
                    "timeout_s": 5.0,
                    "max_events": 20,
                },
            )

        mock_watch.assert_called_once()
        call_kwargs = mock_watch.call_args
        assert call_kwargs.args[0] == "s1"
        assert call_kwargs.kwargs["event_types"] == "snapshot,input_send"
        assert call_kwargs.kwargs["pattern"] == r"\$"
        assert call_kwargs.kwargs["timeout_ms"] == 5000
        assert call_kwargs.kwargs["max_events"] == 20

    async def test_watch_clamps_timeout_to_30s(self) -> None:
        from provide.uterm.server.app import create_server_app

        cfg = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 0},
                "auth": {
                    "mode": "header",
                    "header_mode_acknowledged": True,
                    "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                },
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
        app = create_server_app(cfg)
        mcp = _mcp_for_server(app)

        mock_watch = AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True}))
        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=mock_watch,
        ):
            await _call(mcp, "session_watch", {"session_id": "s1", "timeout_s": 999.0})

        call_kwargs = mock_watch.call_args
        assert call_kwargs.kwargs["timeout_ms"] == 30000  # clamped to 30s


# ---------------------------------------------------------------------------
# HijackClient.watch_session_events — HTTP params
# ---------------------------------------------------------------------------


class TestHijackClientWatchEvents:
    async def test_watch_builds_correct_params(self) -> None:
        from provide.uterm.client.hijack import HijackClient

        recorded: dict[str, Any] = {}

        async def _fake_request(method: str, path: str, *, params: Any = None, **kw: Any) -> tuple[bool, Any]:
            recorded["method"] = method
            recorded["path"] = path
            recorded["params"] = params
            recorded["timeout"] = kw.get("timeout")
            return True, {"events": [], "dropped_count": 0, "timed_out": True}

        client = HijackClient("http://test")
        client._request = _fake_request  # type: ignore[method-assign]

        await client.watch_session_events(
            "s1",
            event_types="snapshot",
            pattern=r"\$",
            timeout_ms=3000,
            max_events=25,
        )

        assert recorded["method"] == "GET"
        assert recorded["path"] == "/api/sessions/s1/events/watch"
        assert recorded["params"]["timeout_ms"] == 3000
        assert recorded["params"]["max_events"] == 25
        assert recorded["params"]["event_types"] == "snapshot"
        assert recorded["params"]["pattern"] == r"\$"
        # Timeout should be server timeout + 5s buffer
        assert recorded["timeout"] == pytest.approx(8.0)

    async def test_watch_omits_none_params(self) -> None:
        from provide.uterm.client.hijack import HijackClient

        recorded: dict[str, Any] = {}

        async def _fake_request(method: str, path: str, *, params: Any = None, **kw: Any) -> tuple[bool, Any]:
            recorded["params"] = params
            return True, {"events": [], "dropped_count": 0, "timed_out": True}

        client = HijackClient("http://test")
        client._request = _fake_request  # type: ignore[method-assign]

        await client.watch_session_events("s1")

        assert "event_types" not in recorded["params"]
        assert "pattern" not in recorded["params"]

    async def test_request_with_timeout_sets_httpx_timeout(self) -> None:
        """Cover hijack.py:120 — _request sets httpx.Timeout when timeout kwarg is provided."""
        from unittest.mock import MagicMock

        import httpx

        from provide.uterm.client.hijack import HijackClient

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.is_success = True
        mock_response.json.return_value = {"ok": True}

        client = HijackClient("http://test")

        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)):
            ok, body = await client._request("GET", "/api/health", timeout=5.0)

        assert ok is True
        assert body == {"ok": True}


# ---------------------------------------------------------------------------
# Registry.watch_session_events — no EventBus fallback
# ---------------------------------------------------------------------------


class TestRegistryWatchEvents:
    async def test_no_event_bus_returns_ring_buffer(self) -> None:
        """When EventBus is not configured, falls back to recent ring-buffer events."""
        hub = TermHub()
        await hub._get("s1")
        for i in range(3):
            await hub.append_event("s1", "snapshot", {"screen": f"line {i}"})

        registry = SessionRegistry([], hub=hub, public_base_url="http://test", recording=RecordingConfig())
        result = await registry.watch_session_events("s1", timeout_ms=100, max_events=10)

        assert result["dropped_count"] == 0
        assert result["timed_out"] is False
        assert len(result["events"]) == 3

    async def test_with_event_bus_receives_event(self) -> None:
        """With EventBus, receives live events and respects max_events."""
        hub = TermHub(event_bus=EventBus())
        await hub._get("s1")
        registry = SessionRegistry([], hub=hub, public_base_url="http://test", recording=RecordingConfig())

        async def _emit_after_delay() -> None:
            await asyncio.sleep(0.05)
            await hub.append_event("s1", "snapshot", {"screen": "hi"})

        task = asyncio.create_task(_emit_after_delay())

        result = await registry.watch_session_events("s1", timeout_ms=2000, max_events=1)
        await task

        assert len(result["events"]) == 1
        assert result["events"][0]["type"] == "snapshot"

    async def test_with_event_bus_timeout(self) -> None:
        """With EventBus but no events, returns timed_out=True after timeout."""
        hub = TermHub(event_bus=EventBus())
        await hub._get("s1")
        registry = SessionRegistry([], hub=hub, public_base_url="http://test", recording=RecordingConfig())

        result = await registry.watch_session_events("s1", timeout_ms=100)

        assert result["timed_out"] is True
        assert result["events"] == []

    async def test_with_event_bus_worker_disconnect_sentinel(self) -> None:
        """Worker disconnect (sentinel) terminates watch cleanly."""
        hub = TermHub(event_bus=EventBus())
        await hub._get("s1")
        registry = SessionRegistry([], hub=hub, public_base_url="http://test", recording=RecordingConfig())

        async def _disconnect_after_delay() -> None:
            await asyncio.sleep(0.05)
            assert hub._event_bus is not None
            hub._event_bus.close_worker("s1")

        task = asyncio.create_task(_disconnect_after_delay())

        result = await registry.watch_session_events("s1", timeout_ms=2000)
        await task

        assert result["timed_out"] is False
        assert result["events"] == []

    async def test_with_event_bus_filters_event_types(self) -> None:
        hub = TermHub(event_bus=EventBus())
        await hub._get("s1")
        registry = SessionRegistry([], hub=hub, public_base_url="http://test", recording=RecordingConfig())

        async def _emit() -> None:
            await asyncio.sleep(0.02)
            await hub.append_event("s1", "input_send", {"keys": "ls\n"})
            await asyncio.sleep(0.02)
            await hub.append_event("s1", "snapshot", {"screen": "file.txt"})

        task = asyncio.create_task(_emit())

        result = await registry.watch_session_events("s1", timeout_ms=2000, event_types=["snapshot"], max_events=1)
        await task

        assert len(result["events"]) == 1
        assert result["events"][0]["type"] == "snapshot"


# ---------------------------------------------------------------------------
# REST endpoint /events/watch
# ---------------------------------------------------------------------------
