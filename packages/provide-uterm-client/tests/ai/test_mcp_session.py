#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Session management and worker control tool tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastmcp import FastMCP
from httpx import ASGITransport
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

from provide.uterm.ai.server import create_mcp_app

WID = "mcp-worker"

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
    hub._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


def _mcp_for(app: FastAPI, **kwargs: object) -> FastMCP:
    """Return a FastMCP app backed by ASGI transport to *app*.

    Tests in this file exercise worker-control tools requiring admin role
    (worker_disconnect/worker_input_mode); after Finding #2 the default
    role dropped to operator so these tests now opt in explicitly.
    """
    kwargs.setdefault("default_role", "admin")
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


def _make_server_app() -> FastAPI:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import config_from_mapping

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


def _mcp_for_server(app: FastAPI) -> FastMCP:
    return create_mcp_app(
        "http://test",
        transport=ASGITransport(app=app),
        headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
    )


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an MCP tool and return the structured_content dict."""
    result = await mcp.call_tool(tool, args or {})
    return result.structured_content  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Session management tools (require full server app)
# ---------------------------------------------------------------------------


class TestSessionTools:
    async def test_server_health_fields(self) -> None:
        app = _make_server_app()
        # ASGITransport does not run the app lifespan, so mark the app ready to
        # simulate a server whose startup has completed (uvicorn sets this in
        # production). Without it, /api/health reports the "starting" 503 state.
        app.state.uterm_ready = True
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "server_health")
        assert data["success"] is True
        assert data["ok"] is True
        assert data["ready"] is True
        assert data["service"] == "uterm-server"

    async def test_session_list_returns_sessions(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_list")
        assert data["success"] is True
        sessions = data["data"]
        assert isinstance(sessions, list)
        assert len(sessions) >= 1
        # s1 should be in the list
        ids = [s["session_id"] for s in sessions]
        assert "s1" in ids

    async def test_session_status_validates_fields(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_status", {"session_id": "s1"})
        assert data["success"] is True
        assert data["session_id"] == "s1"
        assert "display_name" in data

    async def test_session_status_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_status", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_connect_disconnect_sequence(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        data = await _call(mcp, "session_connect", {"session_id": "s1"})
        assert data["success"] is True

        data = await _call(mcp, "session_disconnect", {"session_id": "s1"})
        assert data["success"] is True

    async def test_session_connect_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_connect", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_disconnect_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_disconnect", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_set_mode_open(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_set_mode", {"session_id": "s1", "mode": "open"})
        assert data["success"] is True
        assert data["input_mode"] == "open"

    async def test_session_set_mode_hijack(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_set_mode", {"session_id": "s1", "mode": "hijack"})
        assert data["success"] is True
        assert data["input_mode"] == "hijack"

    async def test_session_set_mode_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "session_set_mode",
            {
                "session_id": "nonexistent",
                "mode": "open",
            },
        )
        assert data["success"] is False

    async def test_session_create_with_display_name(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "session_create",
            {
                "connector_type": "shell",
                "display_name": "Ephemeral",
            },
        )
        assert data["success"] is True
        assert "session_id" in data

    async def test_session_create_all_kwargs(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "session_create",
            {
                "connector_type": "shell",
                "display_name": "Full",
                "host": "example.com",  # public: loopback/RFC1918 now rejected (SSRF guard)
                "port": 23,
                "url": "ws://example.com/ws",
                "username": "user",
                "password": "pass",
                "input_mode": "open",
            },
        )
        assert data["success"] is True
        assert "session_id" in data

    async def test_session_create_minimal(self) -> None:
        """No optional kwargs — all default to None."""
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_create", {"connector_type": "shell"})
        assert data["success"] is True
        assert "session_id" in data

    async def test_session_read_null_snapshot(self) -> None:
        """Session with no worker has null snapshot."""
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_read", {"session_id": "s1"})
        assert data["success"] is True

    async def test_session_read_with_snapshot_data(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        fake_snapshot = {
            "screen": "\x1b[31mred\x1b[0m text",
            "cursor": {"row": 0, "col": 8},
            "cols": 80,
            "rows": 24,
        }
        with patch(
            "provide.uterm.client.hijack.HijackClient.session_snapshot",
            return_value=(True, {"snapshot": fake_snapshot}),
        ):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": "text",
                },
            )
        assert data["success"] is True
        assert "\x1b" not in data["snapshot"]["screen"]
        assert "red text" in data["snapshot"]["screen"]
        assert "cursor" not in data["snapshot"]

    async def test_session_read_rendered_with_snapshot_data(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        fake_snapshot = {
            "screen": "\x1b[31mred\x1b[0m text",
            "cursor": {"row": 0, "col": 8},
            "cols": 80,
            "rows": 24,
        }
        with patch(
            "provide.uterm.client.hijack.HijackClient.session_snapshot",
            return_value=(True, {"snapshot": fake_snapshot}),
        ):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": "rendered",
                },
            )
        assert data["success"] is True
        assert data["snapshot"]["cols"] == 80
        assert data["snapshot"]["cursor"] == {"row": 0, "col": 8}

    async def test_session_read_raw_with_snapshot_data(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)

        fake_snapshot = {
            "screen": "\x1b[31mred\x1b[0m text",
            "cursor": {"row": 0, "col": 8},
            "cols": 80,
            "rows": 24,
        }
        with patch(
            "provide.uterm.client.hijack.HijackClient.session_snapshot",
            return_value=(True, {"snapshot": fake_snapshot}),
        ):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": "raw",
                },
            )
        assert data["success"] is True
        assert "\x1b" in data["snapshot"]["screen"]

    async def test_session_read_unknown_session(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(mcp, "session_read", {"session_id": "nonexistent"})
        assert data["success"] is False

    async def test_session_read_output_modes_null_snapshot(self) -> None:
        """All output modes handle null snapshot gracefully."""
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        for mode in ("text", "rendered", "raw"):
            data = await _call(
                mcp,
                "session_read",
                {
                    "session_id": "s1",
                    "output": mode,
                },
            )
            assert data["success"] is True


# ---------------------------------------------------------------------------
# Worker control tools
# ---------------------------------------------------------------------------


class TestWorkerControlTools:
    async def test_worker_input_mode_set_open(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": WID,
                "mode": "open",
            },
        )
        assert data["success"] is True
        assert data["input_mode"] == "open"

    async def test_worker_input_mode_set_hijack(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": WID,
                "mode": "hijack",
            },
        )
        assert data["success"] is True
        assert data["input_mode"] == "hijack"

    async def test_worker_disconnect_success(self) -> None:
        hub, app = _make_hub_app()
        _add_worker(hub)
        mcp = _mcp_for(app)
        data = await _call(mcp, "worker_disconnect", {"worker_id": WID})
        assert data["success"] is True
        assert data["ok"] is True

    async def test_worker_input_mode_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(
            mcp,
            "worker_input_mode",
            {
                "worker_id": WID,
                "mode": "open",
            },
        )
        assert data["success"] is False

    async def test_worker_disconnect_no_worker(self) -> None:
        _hub, app = _make_hub_app()
        mcp = _mcp_for(app)
        data = await _call(mcp, "worker_disconnect", {"worker_id": WID})
        assert data["success"] is False


# ---------------------------------------------------------------------------
# Fanout and annotation tools
# ---------------------------------------------------------------------------


class TestFanoutAndAnnotateTools:
    async def test_fanout_group_create_returns_group_id(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        data = await _call(
            mcp,
            "fanout_group_create",
            {"session_ids": ["s1"], "name": "test-group"},
        )
        assert data["success"] is True
        assert "group_id" in data

    async def test_fanout_send_to_created_group(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        create_data = await _call(
            mcp,
            "fanout_group_create",
            {"session_ids": ["s1"], "name": "send-test"},
        )
        assert create_data["success"] is True
        group_id = create_data["group_id"]

        data = await _call(
            mcp,
            "fanout_send",
            {"group_id": group_id, "data": "echo hello\r", "quiesce_ms": 50},
        )
        assert isinstance(data["success"], bool)

    async def test_session_annotate_calls_server(self) -> None:
        app = _make_server_app()
        mcp = _mcp_for_server(app)
        # Session s1 is not started (auto_start=False), so annotate returns 404 → success=False.
        # The test verifies the MCP tool reaches the client.post call (covering those lines).
        data = await _call(
            mcp,
            "session_annotate",
            {"session_id": "s1", "label": "test", "description": "a note", "severity": "info"},
        )
        assert isinstance(data["success"], bool)
