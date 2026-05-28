#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the generic manager MCP tools (create_manager_mcp_tools)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.mcp_tools import TOOL_COUNT, create_manager_mcp_tools
from provide.uterm.manager.models import AgentStatusBase
from provide.uterm.manager.process import AgentProcessManager


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "fake.worker"

    def configure_worker_env(self, env, agent_status, manager, **kwargs):
        pass


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
        health_check_interval_s=0,
        spawn_config_dir=str(tmp_path),
    )


@pytest.fixture
def manager(config):
    mgr = AgentManager(config)
    mgr.broadcast_status = AsyncMock()
    return mgr


@pytest.fixture
def pm(manager, tmp_path):
    pm = AgentProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.agent_process_manager = pm
    return pm


@pytest.fixture
def mcp_app(manager, pm):
    return create_manager_mcp_tools(manager)


async def _call(mcp_app, tool_name: str, args: dict | None = None) -> dict:
    """Call a tool on the FastMCP app and return structured_content."""
    result = await mcp_app.call_tool(tool_name, args or {})
    return result.structured_content


# ---------------------------------------------------------------------------
# Factory smoke test
# ---------------------------------------------------------------------------


class TestFactory:
    def test_creates_fastmcp_app(self, mcp_app):
        from fastmcp import FastMCP

        assert isinstance(mcp_app, FastMCP)

    @pytest.mark.asyncio
    async def test_tool_count(self, mcp_app):
        tools = await mcp_app.list_tools()
        assert len(tools) == TOOL_COUNT, f"Expected {TOOL_COUNT} tools, got {len(tools)}: {[t.name for t in tools]}"

    def test_raises_without_manager_or_base_url(self):
        with pytest.raises(ValueError, match="Provide either"):
            create_manager_mcp_tools()

    def test_creates_with_base_url(self):
        from fastmcp import FastMCP

        app = create_manager_mcp_tools(base_url="http://localhost:9999")
        assert isinstance(app, FastMCP)

    @pytest.mark.asyncio
    async def test_base_url_tool_count(self):
        app = create_manager_mcp_tools(base_url="http://localhost:9999")
        tools = await app.list_tools()
        assert len(tools) == TOOL_COUNT

    @pytest.mark.asyncio
    async def test_on_first_http_callback_invoked(self):
        """Test that on_first_http callback is invoked before first HTTP request."""
        from unittest.mock import AsyncMock, patch

        callback_mock = AsyncMock()

        with patch("provide.uterm.manager.mcp_tools._http_request") as mock_http_request:
            # Mock the HTTP request to return swarm status
            mock_http_request.return_value = (
                True,
                {"total_agents": 0, "running_agents": 0, "error_agents": 0, "paused": False},
            )

            app = create_manager_mcp_tools(base_url="http://localhost:9999", on_first_http=callback_mock)

            # Call a tool that makes an HTTP request
            result = await _call(app, "swarm_status")

            # Callback should have been invoked once
            assert callback_mock.call_count == 1
            assert result["total_agents"] == 0

            # Call again to ensure callback is not invoked twice
            result = await _call(app, "swarm_status")
            assert callback_mock.call_count == 1


# ---------------------------------------------------------------------------
# Swarm-level tools
# ---------------------------------------------------------------------------


class TestSwarmStatus:
    @pytest.mark.asyncio
    async def test_returns_status_dict(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "swarm_status")
        assert result["total_agents"] == 1

    @pytest.mark.asyncio
    async def test_telemetry_fields_stripped_by_default(self, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        app = create_manager_mcp_tools(manager, agent_telemetry_fields=frozenset({"custom_telemetry", "extra_data"}))
        # Inject extra fields into the dumped agent data via model
        import unittest.mock as _mock

        with _mock.patch.object(manager, "get_swarm_status") as mock_status:
            mock_status.return_value = _mock.MagicMock()
            mock_status.return_value.model_dump.return_value = {
                "total_agents": 1,
                "agents": [
                    {"agent_id": "agent_000", "state": "running", "custom_telemetry": {"x": 1}, "extra_data": [1, 2]}
                ],
            }
            result = await _call(app, "swarm_status")
        assert "custom_telemetry" not in result["agents"][0]
        assert "extra_data" not in result["agents"][0]
        assert result["agents"][0]["state"] == "running"

    @pytest.mark.asyncio
    async def test_telemetry_fields_preserved_when_include_true(self, manager, pm):
        app = create_manager_mcp_tools(manager, agent_telemetry_fields=frozenset({"custom_telemetry"}))
        import unittest.mock as _mock

        with _mock.patch.object(manager, "get_swarm_status") as mock_status:
            mock_status.return_value = _mock.MagicMock()
            mock_status.return_value.model_dump.return_value = {
                "total_agents": 1,
                "agents": [{"agent_id": "agent_000", "state": "running", "custom_telemetry": {"x": 1}}],
            }
            result = await _call(app, "swarm_status", {"include_telemetry": True})
        assert result["agents"][0]["custom_telemetry"] == {"x": 1}

    @pytest.mark.asyncio
    async def test_no_stripping_when_telemetry_fields_none(self, manager, pm):
        app = create_manager_mcp_tools(manager)  # agent_telemetry_fields=None (default)
        import unittest.mock as _mock

        with _mock.patch.object(manager, "get_swarm_status") as mock_status:
            mock_status.return_value = _mock.MagicMock()
            mock_status.return_value.model_dump.return_value = {
                "total_agents": 1,
                "agents": [{"agent_id": "agent_000", "state": "running", "app_field": "kept"}],
            }
            result = await _call(app, "swarm_status")
        assert result["agents"][0]["app_field"] == "kept"


class TestSwarmPause:
    @pytest.mark.asyncio
    async def test_pauses_swarm(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "swarm_pause")
        assert result["paused"] is True
        assert manager.swarm_paused is True


class TestSwarmResume:
    @pytest.mark.asyncio
    async def test_resumes_swarm(self, mcp_app, manager, pm):
        manager.swarm_paused = True
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_000"].paused = True
        result = await _call(mcp_app, "swarm_resume")
        assert result["paused"] is False
        assert manager.agents["agent_000"].paused is False


class TestSwarmKillAll:
    @pytest.mark.asyncio
    async def test_kills_all(self, mcp_app, manager, pm):
        manager.cancel_spawn = AsyncMock(return_value=False)
        manager.kill_agent = AsyncMock()
        proc = MagicMock()
        proc.pid = 1
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "swarm_kill_all")
        assert result["count"] == 1


class TestSwarmClear:
    @pytest.mark.asyncio
    async def test_clears_all(self, mcp_app, manager, pm):
        manager.cancel_spawn = AsyncMock(return_value=False)
        manager.kill_agent = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "swarm_clear")
        assert result["cleared"] == 1
        assert len(manager.agents) == 0


class TestSwarmPrune:
    @pytest.mark.asyncio
    async def test_prunes_dead(self, mcp_app, manager, pm):
        manager.agents["agent_live"] = AgentStatusBase(agent_id="agent_live", state="running")
        manager.agents["agent_dead"] = AgentStatusBase(agent_id="agent_dead", state="error")
        result = await _call(mcp_app, "swarm_prune")
        assert result["pruned"] == 1
        assert result["remaining"] == 1


class TestSwarmSetDesired:
    @pytest.mark.asyncio
    async def test_sets_desired(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "swarm_set_desired", {"count": 10})
        assert result["desired_agents"] == 10
        assert manager.desired_agents == 10


# ---------------------------------------------------------------------------
# Per-agent tools
# ---------------------------------------------------------------------------


class TestAgentList:
    @pytest.mark.asyncio
    async def test_lists_all_agents(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="error")
        result = await _call(mcp_app, "agent_list")
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_filters_by_state(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="error")
        result = await _call(mcp_app, "agent_list", {"state": "running"})
        assert result["total"] == 1
        assert result["agents"][0]["agent_id"] == "agent_000"


class TestAgentStatus:
    @pytest.mark.asyncio
    async def test_returns_agent(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "agent_status", {"agent_id": "agent_000"})
        assert result["agent_id"] == "agent_000"
        assert result["state"] == "running"

    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "agent_status", {"agent_id": "nope"})
        assert "error" in result


class TestAgentKill:
    @pytest.mark.asyncio
    async def test_kills_agent(self, mcp_app, manager, pm):
        manager.kill_agent = AsyncMock()
        proc = MagicMock()
        proc.pid = 1
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.desired_agents = 5
        result = await _call(mcp_app, "agent_kill", {"agent_id": "agent_000"})
        assert result["action"] == "kill"
        assert manager.desired_agents == 4


class TestAgentPause:
    @pytest.mark.asyncio
    async def test_pauses_agent(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "agent_pause", {"agent_id": "agent_000"})
        assert result["paused"] is True
        assert manager.agents["agent_000"].paused is True


class TestAgentResume:
    @pytest.mark.asyncio
    async def test_resumes_agent(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_000"].paused = True
        result = await _call(mcp_app, "agent_resume", {"agent_id": "agent_000"})
        assert result["paused"] is False
        assert manager.agents["agent_000"].paused is False


class TestAgentRestart:
    @pytest.mark.asyncio
    async def test_queues_restart(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "agent_restart", {"agent_id": "agent_000"})
        assert result["queued"] is True
        assert manager.agents["agent_000"].pending_command_type == "restart"


class TestSwarmSpawnBatch:
    @pytest.mark.asyncio
    async def test_spawns_batch(self, mcp_app, manager, pm, tmp_path):
        cfg = tmp_path / "agent.yaml"
        cfg.write_text("worker_type: default\n")
        manager.start_spawn_swarm = AsyncMock()
        result = await _call(mcp_app, "swarm_spawn_batch", {"config_paths": [str(cfg)], "group_size": 1})
        assert result["status"] == "spawning"
        assert result["total_agents"] == 1
        assert manager.desired_agents == 1

    @pytest.mark.asyncio
    async def test_calculates_groups(self, mcp_app, manager, pm, tmp_path):
        cfg = tmp_path / "agent.yaml"
        cfg.write_text("worker_type: default\n")
        manager.start_spawn_swarm = AsyncMock()
        result = await _call(mcp_app, "swarm_spawn_batch", {"config_paths": [str(cfg)] * 3, "group_size": 2})
        assert result["total_groups"] == 2
        assert result["estimated_time_seconds"] > 0

    @pytest.mark.asyncio
    async def test_rejects_invalid_path(self, mcp_app, manager, pm):
        """Batch spawn must validate paths — not just single spawn."""
        result = await _call(mcp_app, "swarm_spawn_batch", {"config_paths": ["bad.json"]})
        assert "error" in result


class TestAgentKillErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "agent_kill", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_kill_no_process(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.desired_agents = 0  # don't decrement
        result = await _call(mcp_app, "agent_kill", {"agent_id": "agent_000"})
        assert result["state"] == "stopped"


class TestAgentPauseErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "agent_pause", {"agent_id": "nope"})
        assert "error" in result


class TestAgentResumeErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "agent_resume", {"agent_id": "nope"})
        assert "error" in result


class TestAgentRestartErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "agent_restart", {"agent_id": "nope"})
        assert "error" in result


class TestAgentEvents:
    @pytest.mark.asyncio
    async def test_returns_events(self, mcp_app, manager, pm):
        agent = AgentStatusBase(agent_id="agent_000", state="error", error_message="something broke")
        agent.recent_actions = [{"name": "trade", "time": 1.0}]
        manager.agents["agent_000"] = agent
        result = await _call(mcp_app, "agent_events", {"agent_id": "agent_000"})
        assert result["state"] == "error"
        assert len(result["events"]) == 2  # 1 action + 1 error

    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "agent_events", {"agent_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_error_message(self, mcp_app, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await _call(mcp_app, "agent_events", {"agent_id": "agent_000"})
        assert result["state"] == "running"
        assert result["events"] == []
