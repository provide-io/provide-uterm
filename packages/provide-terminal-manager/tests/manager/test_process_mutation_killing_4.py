#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.terminal.manager.process — supplemental batch (part 4).

Classes: TestMonitorBustRespawn, TestMonitorDesiredState.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

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
        return "test_module"

    def configure_worker_env(self, env, agent_status, manager, **kwargs):
        env["CONFIGURED"] = "yes"


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
        health_check_interval_s=0,
        heartbeat_timeout_s=1,
    )


@pytest.fixture
def manager(config):
    return AgentManager(config)


@pytest.fixture
def pm(manager, tmp_path):
    pm = AgentProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.agent_process_manager = pm
    return pm


def make_mock_proc(pid=42, returncode=0):
    m = MagicMock()
    m.pid = pid
    m.returncode = returncode
    m.poll.return_value = None
    m.wait.return_value = returncode
    return m


# ---------------------------------------------------------------------------
# monitor_processes — bust-respawn (mutmut_139-175)
# ---------------------------------------------------------------------------
class TestMonitorBustRespawn:
    async def _run_one(self, pm, manager):
        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_bust_respawn_enabled_running_agent_with_bust_context(self, pm, manager):
        """mutmut_139-142: bust_respawn and not swarm_paused checks."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        agent = AgentStatusBase(agent_id="agent_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(agent, "activity_context", "BUST")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.agents["agent_000"].state == "stopped"
        assert manager.agents["agent_000"].exit_reason == "bust_respawn"

    @pytest.mark.asyncio
    async def test_bust_respawn_skips_when_paused(self, pm, manager):
        """mutmut_140: bust_respawn fires even when swarm_paused -> must not."""
        manager.bust_respawn = True
        manager.swarm_paused = True
        agent = AgentStatusBase(agent_id="agent_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(agent, "activity_context", "BUST")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.agents["agent_000"].state == "running"

    @pytest.mark.asyncio
    async def test_bust_respawn_only_kills_running_agents(self, pm, manager):
        """mutmut_150: state != 'running' check."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        agent = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)
        object.__setattr__(agent, "activity_context", "BUST")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.agents["agent_000"].state == "queued"

    @pytest.mark.asyncio
    async def test_bust_respawn_skips_non_bust_context(self, pm, manager):
        """mutmut_156: ctx != 'BUST' check."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        agent = AgentStatusBase(agent_id="agent_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(agent, "activity_context", "PLAYING")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.agents["agent_000"].state == "running"

    @pytest.mark.asyncio
    async def test_bust_respawn_context_comparison_is_uppercase(self, pm, manager):
        """mutmut_163: 'BUST' comparison (lowercase bust should work too via .upper())."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        agent = AgentStatusBase(agent_id="agent_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(agent, "activity_context", "bust")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.agents["agent_000"].state == "stopped"

    @pytest.mark.asyncio
    async def test_bust_exit_reason_is_bust_respawn(self, pm, manager):
        """mutmut_166: exit_reason='bust_respawn'."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        agent = AgentStatusBase(agent_id="agent_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(agent, "activity_context", "BUST")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.agents["agent_000"].exit_reason == "bust_respawn"

    @pytest.mark.asyncio
    async def test_bust_stopped_at_set(self, pm, manager):
        """mutmut_169/170: stopped_at = now."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        agent = AgentStatusBase(agent_id="agent_000", state="running", pid=0)
        object.__setattr__(agent, "activity_context", "BUST")
        manager.agents["agent_000"] = agent
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        t_before = time.time()
        await self._run_one(pm, manager)
        t_after = time.time()

        assert manager.agents["agent_000"].stopped_at is not None
        assert t_before <= manager.agents["agent_000"].stopped_at <= t_after + 0.1


# ---------------------------------------------------------------------------
# monitor_processes — desired-state enforcement (mutmut_187-334)
# ---------------------------------------------------------------------------
class TestMonitorDesiredState:
    async def _run_one(self, pm, manager):
        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_desired_agents_zero_skips_enforcement(self, pm, manager):
        """mutmut_187: desired_agents > 0 check."""
        manager.desired_agents = 0
        manager.swarm_paused = False
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="error")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_agent", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert len(launched) == 0

    @pytest.mark.asyncio
    async def test_desired_state_skips_when_paused(self, pm, manager, tmp_path):
        """mutmut_189: not swarm_paused check."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_agents = 2
        manager.swarm_paused = True
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_agent", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert len(launched) == 0

    @pytest.mark.asyncio
    async def test_dead_agents_pruned_from_agents(self, pm, manager, tmp_path):
        """mutmut_190-202: dead_agents pruning logic."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_agents = 1
        manager.swarm_paused = False
        manager.agents["agent_error"] = AgentStatusBase(agent_id="agent_error", state="error", config=str(config))
        manager.agents["agent_running"] = AgentStatusBase(agent_id="agent_running", state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_agent", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert "agent_error" not in manager.agents

    @pytest.mark.asyncio
    async def test_active_states_set(self, pm, manager, tmp_path):
        """mutmut_208: active_states = {'running', 'queued', 'recovering', 'blocked'}."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_agents = 3
        manager.swarm_paused = False
        for bid in ["agent_000", "agent_001"]:
            manager.agents[bid] = AgentStatusBase(agent_id=bid, state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def fake_launch(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_agent", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert len(launched) >= 1

    @pytest.mark.asyncio
    async def test_deficit_positive_spawns_agents(self, pm, manager, tmp_path):
        """mutmut_216-228: deficit > 0 branch spawns correct count."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_agents = 3
        manager.swarm_paused = False
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000", state="running", config=str(config), last_update_time=time.time()
        )
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def fake_launch(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_agent", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert len(launched) == 2

    @pytest.mark.asyncio
    async def test_configs_from_active_agents(self, pm, manager, tmp_path):
        """mutmut_225-235: configs_available from active_agents first."""
        config_a = tmp_path / "a.yaml"
        config_a.write_text("worker_type: test_game\n")
        manager.desired_agents = 2
        manager.swarm_paused = False
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000", state="running", config=str(config_a), last_update_time=time.time()
        )
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched_with_config = []

        async def fake_launch(bid, cfg):
            launched_with_config.append((bid, cfg))

        with patch.object(pm, "_launch_queued_agent", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert len(launched_with_config) == 1
        assert launched_with_config[0][1] == str(config_a)

    @pytest.mark.asyncio
    async def test_last_spawn_config_fallback(self, pm, manager, tmp_path):
        """mutmut_237-241: _last_spawn_config fallback when no active/dead configs."""
        config = tmp_path / "last.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_agents = 1
        manager.swarm_paused = False
        pm._last_spawn_config = str(config)
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched_with_config = []

        async def fake_launch(bid, cfg):
            launched_with_config.append(cfg)

        with patch.object(pm, "_launch_queued_agent", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert str(config) in launched_with_config

    @pytest.mark.asyncio
    async def test_queued_status_created_for_new_agent(self, pm, manager, tmp_path):
        """mutmut_243-259: new agent created with state='queued'."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_agents = 2
        manager.swarm_paused = False
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def fake_launch(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_agent", side_effect=fake_launch):
            await self._run_one(pm, manager)

        for bid in launched:
            if bid in manager.agents:
                assert manager.agents[bid].state in ("queued", "running")

    @pytest.mark.asyncio
    async def test_excess_agents_killed_when_over_desired(self, pm, manager):
        """mutmut_266-334: deficit < 0 -> kill excess agents."""
        manager.desired_agents = 1
        manager.swarm_paused = False
        for bid in ["agent_002", "agent_001", "agent_000"]:
            manager.agents[bid] = AgentStatusBase(agent_id=bid, state="running", pid=0, last_update_time=time.time())
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        kill_calls = []

        async def fake_kill(bid):
            kill_calls.append(bid)

        with patch.object(manager, "kill_agent", side_effect=fake_kill):
            await self._run_one(pm, manager)

        assert len(kill_calls) == 2

    @pytest.mark.asyncio
    async def test_excess_sorted_descending_by_agent_id(self, pm, manager):
        """mutmut_291: reverse=True -> kills highest-id agents first."""
        manager.desired_agents = 1
        manager.swarm_paused = False
        for bid in ["agent_000", "agent_001", "agent_002"]:
            manager.agents[bid] = AgentStatusBase(agent_id=bid, state="running", pid=0, last_update_time=time.time())
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        kill_calls = []

        async def fake_kill(bid):
            kill_calls.append(bid)

        with patch.object(manager, "kill_agent", side_effect=fake_kill):
            await self._run_one(pm, manager)

        assert "agent_002" in kill_calls
        assert "agent_001" in kill_calls
        assert "agent_000" not in kill_calls

    @pytest.mark.asyncio
    async def test_excess_agents_removed_from_agents(self, pm, manager):
        """mutmut_306-334: agents.pop and processes.pop called for excess."""
        manager.desired_agents = 1
        manager.swarm_paused = False
        for bid in ["agent_000", "agent_001", "agent_002"]:
            manager.agents[bid] = AgentStatusBase(agent_id=bid, state="running", pid=0)
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        async def fake_kill(bid):
            pass

        with patch.object(manager, "kill_agent", side_effect=fake_kill):
            await self._run_one(pm, manager)

        remaining = len(manager.agents)
        assert remaining <= 2
