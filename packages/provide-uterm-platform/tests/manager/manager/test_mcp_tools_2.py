#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HTTP-mode and _http_request tests for create_manager_mcp_tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.manager.mcp_tools import create_manager_mcp_tools


async def _call(mcp_app, tool_name: str, args: dict | None = None) -> dict:
    """Call a tool on the FastMCP app and return structured_content."""
    result = await mcp_app.call_tool(tool_name, args or {})
    return result.structured_content


# ---------------------------------------------------------------------------
# _http_request helper
# ---------------------------------------------------------------------------


class TestHttpRequest:
    """Cover the _http_request helper function (lines 35-45)."""

    @pytest.mark.asyncio
    async def test_success_dict_response(self) -> None:
        from provide.uterm.manager.mcp_tools import _http_request

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, data = await _http_request("http://x", "GET", "/path")
        assert ok is True
        assert data == {"ok": True}

    @pytest.mark.asyncio
    async def test_success_non_dict_response(self) -> None:
        from provide.uterm.manager.mcp_tools import _http_request

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = ["a", "b"]

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, data = await _http_request("http://x", "GET", "/path")
        assert ok is True
        assert data == {"value": ["a", "b"]}

    @pytest.mark.asyncio
    async def test_error_status_dict(self) -> None:
        from provide.uterm.manager.mcp_tools import _http_request

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"error": "not found"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, data = await _http_request("http://x", "GET", "/path")
        assert ok is False
        assert data == {"error": "not found"}

    @pytest.mark.asyncio
    async def test_error_status_non_dict(self) -> None:
        from provide.uterm.manager.mcp_tools import _http_request

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = "internal error"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, data = await _http_request("http://x", "GET", "/path")
        assert ok is False
        assert "error" in data

    @pytest.mark.asyncio
    async def test_network_exception(self) -> None:
        from provide.uterm.manager.mcp_tools import _http_request

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(side_effect=ConnectionError("refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, data = await _http_request("http://x", "GET", "/path")
        assert ok is False
        assert "refused" in data["error"]


# ---------------------------------------------------------------------------
# HTTP mode (base_url) tools
# ---------------------------------------------------------------------------


@pytest.fixture
def http_app():
    return create_manager_mcp_tools(base_url="http://manager:2272")


def _mock_http(ok: bool, data: dict) -> AsyncMock:
    """Patch _http_request to return a fixed (ok, data) tuple."""
    return patch(
        "provide.uterm.manager.mcp_tools._http_request",
        new=AsyncMock(return_value=(ok, data)),
    )


class TestHttpModeSwarm:
    @pytest.mark.asyncio
    async def test_swarm_status_ok(self, http_app) -> None:
        with _mock_http(True, {"total_agents": 3}):
            result = await _call(http_app, "swarm_status")
        assert result["total_agents"] == 3

    @pytest.mark.asyncio
    async def test_swarm_status_error(self, http_app) -> None:
        with _mock_http(False, {"error": "conn refused"}):
            result = await _call(http_app, "swarm_status")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_spawn_batch_ok(self, http_app) -> None:
        with _mock_http(True, {"status": "spawning"}):
            result = await _call(http_app, "swarm_spawn_batch", {"config_paths": ["/a.yaml"]})
        assert result["status"] == "spawning"

    @pytest.mark.asyncio
    async def test_swarm_spawn_batch_error(self, http_app) -> None:
        with _mock_http(False, {"error": "fail"}):
            result = await _call(http_app, "swarm_spawn_batch", {"config_paths": ["/a.yaml"]})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_pause_ok(self, http_app) -> None:
        with _mock_http(True, {"paused": True}):
            result = await _call(http_app, "swarm_pause")
        assert result["paused"] is True

    @pytest.mark.asyncio
    async def test_swarm_pause_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "swarm_pause")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_resume_ok(self, http_app) -> None:
        with _mock_http(True, {"paused": False}):
            result = await _call(http_app, "swarm_resume")
        assert result["paused"] is False

    @pytest.mark.asyncio
    async def test_swarm_resume_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "swarm_resume")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_kill_all_ok(self, http_app) -> None:
        with _mock_http(True, {"count": 2}):
            result = await _call(http_app, "swarm_kill_all")
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_swarm_kill_all_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "swarm_kill_all")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_clear_ok(self, http_app) -> None:
        with _mock_http(True, {"cleared": 1}):
            result = await _call(http_app, "swarm_clear")
        assert result["cleared"] == 1

    @pytest.mark.asyncio
    async def test_swarm_clear_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "swarm_clear")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_prune_ok(self, http_app) -> None:
        with _mock_http(True, {"pruned": 1}):
            result = await _call(http_app, "swarm_prune")
        assert result["pruned"] == 1

    @pytest.mark.asyncio
    async def test_swarm_prune_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "swarm_prune")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_swarm_set_desired_ok(self, http_app) -> None:
        with _mock_http(True, {"desired_agents": 5}):
            result = await _call(http_app, "swarm_set_desired", {"count": 5})
        assert result["desired_agents"] == 5

    @pytest.mark.asyncio
    async def test_swarm_set_desired_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "swarm_set_desired", {"count": 5})
        assert "error" in result


class TestHttpModeAgents:
    @pytest.mark.asyncio
    async def test_agent_list_ok(self, http_app) -> None:
        with _mock_http(True, {"total": 1, "agents": []}):
            result = await _call(http_app, "agent_list")
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_agent_list_with_state_ok(self, http_app) -> None:
        with _mock_http(True, {"total": 0, "agents": []}):
            result = await _call(http_app, "agent_list", {"state": "running"})
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_agent_list_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "agent_list")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_agent_status_ok(self, http_app) -> None:
        with _mock_http(True, {"agent_id": "agent_000", "state": "running"}):
            result = await _call(http_app, "agent_status", {"agent_id": "agent_000"})
        assert result["agent_id"] == "agent_000"

    @pytest.mark.asyncio
    async def test_agent_status_error(self, http_app) -> None:
        with _mock_http(False, {"error": "not found"}):
            result = await _call(http_app, "agent_status", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_agent_kill_ok(self, http_app) -> None:
        with _mock_http(True, {"agent_id": "agent_000", "action": "kill"}):
            result = await _call(http_app, "agent_kill", {"agent_id": "agent_000"})
        assert result["action"] == "kill"

    @pytest.mark.asyncio
    async def test_agent_kill_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "agent_kill", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_agent_pause_ok(self, http_app) -> None:
        with _mock_http(True, {"paused": True}):
            result = await _call(http_app, "agent_pause", {"agent_id": "agent_000"})
        assert result["paused"] is True

    @pytest.mark.asyncio
    async def test_agent_pause_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "agent_pause", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_agent_resume_ok(self, http_app) -> None:
        with _mock_http(True, {"paused": False}):
            result = await _call(http_app, "agent_resume", {"agent_id": "agent_000"})
        assert result["paused"] is False

    @pytest.mark.asyncio
    async def test_agent_resume_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "agent_resume", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_agent_restart_ok(self, http_app) -> None:
        with _mock_http(True, {"queued": True}):
            result = await _call(http_app, "agent_restart", {"agent_id": "agent_000"})
        assert result["queued"] is True

    @pytest.mark.asyncio
    async def test_agent_restart_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "agent_restart", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_agent_events_ok(self, http_app) -> None:
        with _mock_http(True, {"agent_id": "agent_000", "events": []}):
            result = await _call(http_app, "agent_events", {"agent_id": "agent_000"})
        assert result["agent_id"] == "agent_000"

    @pytest.mark.asyncio
    async def test_agent_events_error(self, http_app) -> None:
        with _mock_http(False, {"error": "x"}):
            result = await _call(http_app, "agent_events", {"agent_id": "nope"})
        assert "error" in result
