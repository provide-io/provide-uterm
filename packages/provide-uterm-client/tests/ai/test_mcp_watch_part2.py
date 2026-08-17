#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the session_watch MCP tool and the /events/watch REST endpoint."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.config import config_from_mapping

from provide.uterm.ai.server import create_mcp_app

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


def _mcp_for_server(app: FastAPI) -> MCPServer:
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
    )


async def _call(mcp: MCPServer, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(tool, args or {})
    # MCPServer.call_tool() returns CallToolResult | InputRequiredResult; only
    # the former carries structured_content. None of these tests exercise the
    # elicitation path that produces InputRequiredResult, so a plain isinstance
    # narrows the union for mypy without changing runtime behavior.
    assert isinstance(result, CallToolResult)
    return result.structured_content  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# TOOL_COUNT sanity check
# ---------------------------------------------------------------------------


class TestWatchEndpoint:
    def _make_app_with_bus(self) -> tuple[FastAPI, TermHub, EventBus]:
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
        bus = EventBus()
        return app, None, bus  # hub injected post-lifespan in tests

    async def test_watch_endpoint_no_bus_returns_empty(self) -> None:
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
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
        ) as client:
            r = await client.get("/api/sessions/s1/events/watch", params={"timeout_ms": 100})
        assert r.status_code == 200
        body = r.json()
        assert "events" in body
        assert body["dropped_count"] == 0
        # No event bus → poll times out; timed_out may be True or False depending on timing
        assert isinstance(body["timed_out"], bool)

    async def test_watch_endpoint_authz_enforced(self) -> None:
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
                        "visibility": "private",
                        "auto_start": False,
                    }
                ],
            }
        )
        app = create_server_app(cfg)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Uterm-Principal": "other-user", "X-Uterm-Role": "viewer"},
        ) as client:
            r = await client.get("/api/sessions/s1/events/watch")
        assert r.status_code == 403

    async def test_watch_endpoint_with_bus_receives_event(self) -> None:
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
        bus = EventBus()

        # Inject EventBus into the hub via app.state after startup
        async def _startup() -> None:
            registry = app.state.uterm_registry
            registry._hub._event_bus = bus
            await registry._hub._get("s1")

        # Run a background task that emits an event after a short delay
        async def _emit() -> None:
            await asyncio.sleep(0.05)
            await app.state.uterm_registry._hub.append_event("s1", "snapshot", {"screen": "hi"})

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
            timeout=10.0,
        ) as client:
            # Startup: inject bus
            await _startup()
            # Schedule event emission
            task = asyncio.create_task(_emit())
            r = await client.get(
                "/api/sessions/s1/events/watch",
                params={"timeout_ms": 2000, "max_events": 1},
            )
            await task

        assert r.status_code == 200
        body = r.json()
        assert len(body["events"]) == 1
        assert body["events"][0]["type"] == "snapshot"


# ---------------------------------------------------------------------------
# session_subscribe MCP tool
# ---------------------------------------------------------------------------


class TestSessionSubscribeMcpTool:
    def _make_app(self) -> FastAPI:
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
        return create_server_app(cfg)

    async def test_subscribe_returns_events_and_matched_pattern_false_when_no_pattern(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)

        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "events": [{"worker_id": "s1", "seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}],
                        "dropped_count": 0,
                        "timed_out": False,
                    },
                )
            ),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1"})

        assert data["success"] is True
        assert len(data["events"]) == 1
        assert data["matched_pattern"] is False  # no pattern given

    async def test_subscribe_matched_pattern_true_when_pattern_and_events(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)

        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "events": [{"type": "snapshot", "data": {"screen": "$ "}}],
                        "dropped_count": 0,
                        "timed_out": False,
                    },
                )
            ),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1", "pattern": r"\$ "})

        assert data["matched_pattern"] is True

    async def test_subscribe_matched_pattern_false_when_no_events(self) -> None:
        """Pattern given but no events returned → matched_pattern=False."""
        app = self._make_app()
        mcp = _mcp_for_server(app)

        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True})),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1", "pattern": r"\$ "})

        assert data["matched_pattern"] is False

    async def test_subscribe_matched_pattern_false_when_events_dont_match(self) -> None:
        """Finding #11: events arrived but none of them match the pattern → False.

        Regression: the prior implementation reported True whenever any event
        arrived, regardless of whether its screen text matched the pattern.
        This matters on the registry fallback path (no EventBus) which does
        not pre-filter events server-side.
        """
        app = self._make_app()
        mcp = _mcp_for_server(app)

        events = [
            {"type": "snapshot", "data": {"screen": "running..."}},
            {"type": "input_send", "data": {"keys": "ls\n"}},
        ]
        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(return_value=(True, {"events": events, "dropped_count": 0, "timed_out": True})),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1", "pattern": r"\$ "})

        assert data["matched_pattern"] is False

    async def test_subscribe_invalid_pattern_is_rejected(self) -> None:
        # An invalid regex now fails closed with ``invalid_pattern`` (ReDoS
        # hardening) rather than being silently ignored.
        app = self._make_app()
        mcp = _mcp_for_server(app)

        data = await _call(mcp, "session_subscribe", {"session_id": "s1", "pattern": "["})

        assert data["success"] is False
        assert data["error"] == "invalid_pattern"

    async def test_subscribe_skips_non_dict_events_and_coerces_screen(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)

        events = [
            "not-a-dict",
            {"type": "snapshot", "data": "not-a-payload"},
            {"type": "snapshot", "data": {"screen": 12345}},
        ]
        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(return_value=(True, {"events": events, "dropped_count": 0, "timed_out": False})),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1", "pattern": "12345"})

        assert data["matched_pattern"] is True

    async def test_subscribe_matched_pattern_true_when_one_event_matches(self) -> None:
        """A single matching snapshot among many non-matching events fires the flag."""
        app = self._make_app()
        mcp = _mcp_for_server(app)

        events = [
            {"type": "snapshot", "data": {"screen": "loading"}},
            {"type": "snapshot", "data": {"screen": "user@host:~$ "}},
            {"type": "snapshot", "data": {"screen": "also non-matching"}},
        ]
        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(return_value=(True, {"events": events, "dropped_count": 0, "timed_out": False})),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1", "pattern": r"\$ "})

        assert data["matched_pattern"] is True

    async def test_subscribe_clamps_duration_min(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)
        mock_watch = AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True}))
        with patch("provide.uterm.client.hijack.HijackClient.watch_session_events", new=mock_watch):
            await _call(mcp, "session_subscribe", {"session_id": "s1", "duration_s": 0.001})

        call_kwargs = mock_watch.call_args
        assert call_kwargs.kwargs["timeout_ms"] == 1000  # min 1 s

    async def test_subscribe_clamps_duration_max(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)
        mock_watch = AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True}))
        with patch("provide.uterm.client.hijack.HijackClient.watch_session_events", new=mock_watch):
            await _call(mcp, "session_subscribe", {"session_id": "s1", "duration_s": 9999.0})

        call_kwargs = mock_watch.call_args
        assert call_kwargs.kwargs["timeout_ms"] == 120000  # max 120 s

    async def test_subscribe_clamps_max_events_min(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)
        mock_watch = AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True}))
        with patch("provide.uterm.client.hijack.HijackClient.watch_session_events", new=mock_watch):
            await _call(mcp, "session_subscribe", {"session_id": "s1", "max_events": 0})

        call_kwargs = mock_watch.call_args
        assert call_kwargs.kwargs["max_events"] == 1  # min 1

    async def test_subscribe_clamps_max_events_max(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)
        mock_watch = AsyncMock(return_value=(True, {"events": [], "dropped_count": 0, "timed_out": True}))
        with patch("provide.uterm.client.hijack.HijackClient.watch_session_events", new=mock_watch):
            await _call(mcp, "session_subscribe", {"session_id": "s1", "max_events": 10000})

        call_kwargs = mock_watch.call_args
        assert call_kwargs.kwargs["max_events"] == 500  # max 500

    async def test_subscribe_client_error_returns_failure(self) -> None:
        app = self._make_app()
        mcp = _mcp_for_server(app)

        with patch(
            "provide.uterm.client.hijack.HijackClient.watch_session_events",
            new=AsyncMock(return_value=(False, {"error": "timeout"})),
        ):
            data = await _call(mcp, "session_subscribe", {"session_id": "s1"})

        assert data["success"] is False
        assert data["matched_pattern"] is False
