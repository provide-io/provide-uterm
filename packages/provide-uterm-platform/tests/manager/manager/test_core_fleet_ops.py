#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for AgentManager fleet operations (kill_all, clear_swarm, prune_dead, pause/resume)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import AgentManager
from provide.terminal.manager.models import AgentStatusBase
from provide.terminal.manager.process import AgentProcessManager


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


# ---------------------------------------------------------------------------
# pause_swarm / resume_swarm
# ---------------------------------------------------------------------------


class TestPauseSwarm:
    @pytest.mark.asyncio
    async def test_pause_sets_flag_and_pauses_active_agents(self, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="stopped")
        manager.agents["agent_002"] = AgentStatusBase(agent_id="agent_002", state="recovering")

        result = await manager.pause_swarm()

        assert manager.swarm_paused is True
        assert manager.agents["agent_000"].paused is True
        assert manager.agents["agent_001"].paused is False  # stopped — not paused
        assert manager.agents["agent_002"].paused is True
        assert result["paused"] is True
        assert result["affected"] == 2
        manager.broadcast_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pause_with_no_agents(self, manager, pm):
        result = await manager.pause_swarm()
        assert result["affected"] == 0
        assert manager.swarm_paused is True


class TestResumeSwarm:
    @pytest.mark.asyncio
    async def test_resume_unsets_flag_and_unpauses_agents(self, manager, pm):
        manager.swarm_paused = True
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_000"].paused = True
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="running")
        manager.agents["agent_001"].paused = True

        result = await manager.resume_swarm()

        assert manager.swarm_paused is False
        assert manager.agents["agent_000"].paused is False
        assert manager.agents["agent_001"].paused is False
        assert result["resumed"] == 2
        assert result["paused"] is False
        manager.broadcast_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_with_no_paused_agents(self, manager, pm):
        manager.swarm_paused = True
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await manager.resume_swarm()
        assert result["resumed"] == 0


# ---------------------------------------------------------------------------
# kill_all
# ---------------------------------------------------------------------------


class TestKillAll:
    @pytest.mark.asyncio
    async def test_kills_all_processes(self, manager, pm):
        proc_mock = MagicMock()
        proc_mock.pid = 42
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", pid=42)
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="running", pid=43)
        manager.processes["agent_000"] = proc_mock
        manager.processes["agent_001"] = proc_mock

        killed_ids = []

        async def tracking_kill(agent_id):
            killed_ids.append(agent_id)
            # Simulate kill_agent removing from processes
            manager.processes.pop(agent_id, None)

        manager.kill_agent = tracking_kill
        manager.cancel_spawn = AsyncMock(return_value=False)

        result = await manager.kill_all()

        assert set(result["killed"]) == {"agent_000", "agent_001"}
        assert result["count"] == 2
        manager.cancel_spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_all_with_no_processes(self, manager, pm):
        manager.cancel_spawn = AsyncMock(return_value=False)
        result = await manager.kill_all()
        assert result["killed"] == []
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# clear_swarm
# ---------------------------------------------------------------------------


class TestClearSwarm:
    @pytest.mark.asyncio
    async def test_clear_removes_all_agents(self, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="stopped")
        manager.cancel_spawn = AsyncMock(return_value=False)
        manager.kill_agent = AsyncMock()

        result = await manager.clear_swarm()

        assert result["cleared"] == 2
        assert len(manager.agents) == 0
        assert len(manager.processes) == 0
        manager.broadcast_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# prune_dead
# ---------------------------------------------------------------------------


class TestPruneDead:
    @pytest.mark.asyncio
    async def test_prune_removes_terminal_agents(self, manager, pm):
        manager.agents["agent_running"] = AgentStatusBase(agent_id="agent_running", state="running")
        manager.agents["agent_stopped"] = AgentStatusBase(agent_id="agent_stopped", state="stopped")
        manager.agents["agent_error"] = AgentStatusBase(agent_id="agent_error", state="error")
        manager.agents["agent_done"] = AgentStatusBase(agent_id="agent_done", state="completed")

        result = await manager.prune_dead()

        assert result["pruned"] == 3
        assert result["remaining"] == 1
        assert "agent_running" in manager.agents
        assert "agent_stopped" not in manager.agents
        assert "agent_error" not in manager.agents
        assert "agent_done" not in manager.agents
        manager.broadcast_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prune_with_no_dead_agents(self, manager, pm):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        result = await manager.prune_dead()
        assert result["pruned"] == 0
        assert result["remaining"] == 1
