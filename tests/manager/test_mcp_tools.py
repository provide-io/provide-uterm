#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the generic manager MCP tools (create_manager_mcp_tools)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.mcp_tools import TOOL_COUNT, create_manager_mcp_tools
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.process import BotProcessManager


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "fake.worker"

    def configure_worker_env(self, env, bot_status, manager, **kwargs):
        pass


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
        health_check_interval_s=0,
    )


@pytest.fixture
def manager(config):
    mgr = SwarmManager(config)
    mgr.broadcast_status = AsyncMock()
    return mgr


@pytest.fixture
def pm(manager, tmp_path):
    pm = BotProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.bot_process_manager = pm
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


# ---------------------------------------------------------------------------
# Swarm-level tools
# ---------------------------------------------------------------------------


class TestSwarmStatus:
    @pytest.mark.asyncio
    async def test_returns_status_dict(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "swarm_status")
        assert result["total_bots"] == 1


class TestSwarmPause:
    @pytest.mark.asyncio
    async def test_pauses_swarm(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "swarm_pause")
        assert result["paused"] is True
        assert manager.swarm_paused is True


class TestSwarmResume:
    @pytest.mark.asyncio
    async def test_resumes_swarm(self, mcp_app, manager, pm):
        manager.swarm_paused = True
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_000"].paused = True
        result = await _call(mcp_app, "swarm_resume")
        assert result["paused"] is False
        assert manager.bots["bot_000"].paused is False


class TestSwarmKillAll:
    @pytest.mark.asyncio
    async def test_kills_all(self, mcp_app, manager, pm):
        manager.cancel_spawn = AsyncMock(return_value=False)
        manager.kill_bot = AsyncMock()
        proc = MagicMock()
        proc.pid = 1
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "swarm_kill_all")
        assert result["count"] == 1


class TestSwarmClear:
    @pytest.mark.asyncio
    async def test_clears_all(self, mcp_app, manager, pm):
        manager.cancel_spawn = AsyncMock(return_value=False)
        manager.kill_bot = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "swarm_clear")
        assert result["cleared"] == 1
        assert len(manager.bots) == 0


class TestSwarmPrune:
    @pytest.mark.asyncio
    async def test_prunes_dead(self, mcp_app, manager, pm):
        manager.bots["bot_live"] = BotStatusBase(bot_id="bot_live", state="running")
        manager.bots["bot_dead"] = BotStatusBase(bot_id="bot_dead", state="error")
        result = await _call(mcp_app, "swarm_prune")
        assert result["pruned"] == 1
        assert result["remaining"] == 1


class TestSwarmSetDesired:
    @pytest.mark.asyncio
    async def test_sets_desired(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "swarm_set_desired", {"count": 10})
        assert result["desired_bots"] == 10
        assert manager.desired_bots == 10


# ---------------------------------------------------------------------------
# Per-bot tools
# ---------------------------------------------------------------------------


class TestBotList:
    @pytest.mark.asyncio
    async def test_lists_all_bots(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="error")
        result = await _call(mcp_app, "bot_list")
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_filters_by_state(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="error")
        result = await _call(mcp_app, "bot_list", {"state": "running"})
        assert result["total"] == 1
        assert result["bots"][0]["bot_id"] == "bot_000"


class TestBotStatus:
    @pytest.mark.asyncio
    async def test_returns_bot(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "bot_status", {"bot_id": "bot_000"})
        assert result["bot_id"] == "bot_000"
        assert result["state"] == "running"

    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "bot_status", {"bot_id": "nope"})
        assert "error" in result


class TestBotKill:
    @pytest.mark.asyncio
    async def test_kills_bot(self, mcp_app, manager, pm):
        manager.kill_bot = AsyncMock()
        proc = MagicMock()
        proc.pid = 1
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.desired_bots = 5
        result = await _call(mcp_app, "bot_kill", {"bot_id": "bot_000"})
        assert result["action"] == "kill"
        assert manager.desired_bots == 4


class TestBotPause:
    @pytest.mark.asyncio
    async def test_pauses_bot(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "bot_pause", {"bot_id": "bot_000"})
        assert result["paused"] is True
        assert manager.bots["bot_000"].paused is True


class TestBotResume:
    @pytest.mark.asyncio
    async def test_resumes_bot(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_000"].paused = True
        result = await _call(mcp_app, "bot_resume", {"bot_id": "bot_000"})
        assert result["paused"] is False
        assert manager.bots["bot_000"].paused is False


class TestBotRestart:
    @pytest.mark.asyncio
    async def test_queues_restart(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "bot_restart", {"bot_id": "bot_000"})
        assert result["queued"] is True
        assert manager.bots["bot_000"].pending_command_type == "restart"


class TestSwarmSpawnBatch:
    @pytest.mark.asyncio
    async def test_spawns_batch(self, mcp_app, manager, pm, tmp_path):
        cfg = tmp_path / "bot.yaml"
        cfg.write_text("worker_type: default\n")
        manager.start_spawn_swarm = AsyncMock()
        result = await _call(mcp_app, "swarm_spawn_batch", {"config_paths": [str(cfg)], "group_size": 1})
        assert result["status"] == "spawning"
        assert result["total_bots"] == 1
        assert manager.desired_bots == 1

    @pytest.mark.asyncio
    async def test_calculates_groups(self, mcp_app, manager, pm, tmp_path):
        cfg = tmp_path / "bot.yaml"
        cfg.write_text("worker_type: default\n")
        manager.start_spawn_swarm = AsyncMock()
        result = await _call(mcp_app, "swarm_spawn_batch", {"config_paths": [str(cfg)] * 3, "group_size": 2})
        assert result["total_groups"] == 2
        assert result["estimated_time_seconds"] > 0


class TestBotKillErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "bot_kill", {"bot_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_kill_no_process(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.desired_bots = 0  # don't decrement
        result = await _call(mcp_app, "bot_kill", {"bot_id": "bot_000"})
        assert result["state"] == "stopped"


class TestBotPauseErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "bot_pause", {"bot_id": "nope"})
        assert "error" in result


class TestBotResumeErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "bot_resume", {"bot_id": "nope"})
        assert "error" in result


class TestBotRestartErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "bot_restart", {"bot_id": "nope"})
        assert "error" in result


class TestBotEvents:
    @pytest.mark.asyncio
    async def test_returns_events(self, mcp_app, manager, pm):
        bot = BotStatusBase(bot_id="bot_000", state="error", error_message="something broke")
        bot.recent_actions = [{"name": "trade", "time": 1.0}]
        manager.bots["bot_000"] = bot
        result = await _call(mcp_app, "bot_events", {"bot_id": "bot_000"})
        assert result["state"] == "error"
        assert len(result["events"]) == 2  # 1 action + 1 error

    @pytest.mark.asyncio
    async def test_not_found(self, mcp_app, manager, pm):
        result = await _call(mcp_app, "bot_events", {"bot_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_error_message(self, mcp_app, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await _call(mcp_app, "bot_events", {"bot_id": "bot_000"})
        assert result["state"] == "running"
        assert result["events"] == []
