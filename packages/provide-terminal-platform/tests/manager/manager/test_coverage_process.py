#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for manager/process.py — monitor loop, spawn, and branch arcs."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import provide.terminal.manager.process as process_module
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
        pass


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


class TestProcessMonitorLoop:
    """Cover lines 347-512: monitor_processes full loop."""

    @pytest.fixture
    def setup(self, pm, manager):
        manager.broadcast_status = AsyncMock()
        manager.health_check_interval = 0
        return pm, manager

    @pytest.mark.asyncio
    async def test_monitor_detects_exited_process_success(self, setup):
        pm, manager = setup
        proc = MagicMock()
        proc.poll.side_effect = [0, None]
        proc.returncode = 0
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.agents["agent_000"].state == "completed"
        assert manager.agents["agent_000"].exit_reason == "target_reached"

    @pytest.mark.asyncio
    async def test_monitor_detects_exited_process_error(self, setup):
        pm, manager = setup
        proc = MagicMock()
        proc.poll.side_effect = [1, None]
        proc.returncode = 1
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.agents["agent_000"].state == "error"
        assert manager.agents["agent_000"].exit_reason == "exit_code_1"

    @pytest.mark.asyncio
    async def test_monitor_exited_with_prior_error(self, setup):
        pm, manager = setup
        proc = MagicMock()
        proc.poll.side_effect = [0, None]
        proc.returncode = 0
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="error",
            error_message="earlier error",
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.agents["agent_000"].exit_reason == "reported_error_then_exit_0"

    @pytest.mark.asyncio
    async def test_monitor_exited_no_agent_entry(self, setup):
        pm, manager = setup
        proc = MagicMock()
        proc.poll.side_effect = [0, None]
        proc.returncode = 0
        manager.processes["orphan"] = proc

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "orphan" not in manager.processes

    @pytest.mark.asyncio
    async def test_monitor_heartbeat_timeout(self, setup):
        pm, manager = setup
        manager.config.heartbeat_timeout_s = 0.01
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="running",
            last_update_time=0,
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.agents["agent_000"].state == "error"
        assert manager.agents["agent_000"].exit_reason == "heartbeat_timeout"

    @pytest.mark.asyncio
    async def test_monitor_heartbeat_with_process(self, setup):
        pm, manager = setup
        manager.config.heartbeat_timeout_s = 0.01
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="running",
            last_update_time=0,
        )

        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as mock_stop:
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        mock_stop.assert_awaited_once_with(agent_id="agent_000", process=proc, timeout_s=process_module._STOP_TIMEOUT_S)
        assert "agent_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_monitor_stale_queued_no_desired(self, setup):
        pm, manager = setup
        pm._queued_launch_delay = 0.01
        manager.desired_agents = 0
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="queued",
            pid=0,
            config="/c.yaml",
        )
        pm._queued_since["agent_000"] = time.time() - 100

        with patch.object(pm, "_launch_queued_agent", new_callable=AsyncMock) as mock_launch:
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            mock_launch.assert_called()

    @pytest.mark.asyncio
    async def test_monitor_stale_queued_with_desired(self, setup):
        pm, manager = setup
        pm._queued_launch_delay = 0.01
        manager.desired_agents = 5
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="queued",
            pid=0,
            config="/c.yaml",
        )
        pm._queued_since["agent_000"] = time.time() - 100

        with patch.object(pm, "_launch_queued_agent", new_callable=AsyncMock):
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_monitor_stale_queued_no_config(self, setup):
        pm, manager = setup
        pm._queued_launch_delay = 0.01
        manager.desired_agents = 0
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="queued",
            pid=0,
            config="",
        )
        pm._queued_since["agent_000"] = time.time() - 100

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.agents["agent_000"].state == "stopped"
        assert manager.agents["agent_000"].exit_reason == "no_config"

    @pytest.mark.asyncio
    async def test_monitor_stale_queued_first_seen(self, setup):
        pm, manager = setup
        pm._queued_launch_delay = 999
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="queued",
            pid=0,
            config="/c.yaml",
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "agent_000" in pm._queued_since

    @pytest.mark.asyncio
    async def test_monitor_bust_respawn(self, setup):
        pm, manager = setup
        manager.bust_respawn = True

        class AgentWithActivity(AgentStatusBase):
            activity_context: str | None = None

        manager.agents["agent_000"] = AgentWithActivity(
            agent_id="agent_000",
            state="running",
            activity_context="BUST",
            last_update_time=time.time(),
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.agents["agent_000"].state == "stopped"
        assert manager.agents["agent_000"].exit_reason == "bust_respawn"

    @pytest.mark.asyncio
    async def test_monitor_bust_respawn_with_process(self, setup):
        pm, manager = setup
        manager.bust_respawn = True
        proc = MagicMock()
        proc.poll.return_value = None
        manager.processes["agent_000"] = proc

        class AgentWithActivity(AgentStatusBase):
            activity_context: str | None = None

        manager.agents["agent_000"] = AgentWithActivity(
            agent_id="agent_000",
            state="running",
            activity_context="BUST",
            last_update_time=time.time(),
        )

        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as mock_stop:
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        mock_stop.assert_awaited_once_with(
            agent_id="agent_000", process=proc, pid=None, timeout_s=process_module._STOP_TIMEOUT_S
        )

    @pytest.mark.asyncio
    async def test_monitor_bust_respawn_by_pid(self, setup):
        pm, manager = setup
        manager.bust_respawn = True

        class AgentWithActivity(AgentStatusBase):
            activity_context: str | None = None

        manager.agents["agent_000"] = AgentWithActivity(
            agent_id="agent_000",
            state="running",
            activity_context="BUST",
            pid=99999,
            last_update_time=time.time(),
        )

        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as mock_stop:
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            mock_stop.assert_awaited_once_with(
                agent_id="agent_000",
                process=None,
                pid=99999,
                timeout_s=process_module._STOP_TIMEOUT_S,
            )

    @pytest.mark.asyncio
    async def test_monitor_desired_state_prune_and_spawn(self, setup):
        pm, manager = setup
        manager.desired_agents = 2
        manager.agents["dead"] = AgentStatusBase(agent_id="dead", state="error", config="/c.yaml")
        manager.agents["alive"] = AgentStatusBase(agent_id="alive", state="running", config="/c.yaml")

        with patch.object(pm, "_launch_queued_agent", new_callable=AsyncMock):
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert "dead" not in manager.agents

    @pytest.mark.asyncio
    async def test_monitor_desired_state_scale_down(self, setup):
        pm, manager = setup
        manager.desired_agents = 1
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001", state="running")

        with patch.object(manager, "kill_agent", new_callable=AsyncMock):
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(manager.agents) <= 1

    @pytest.mark.asyncio
    async def test_monitor_desired_state_no_config_available(self, setup):
        pm, manager = setup
        manager.desired_agents = 2
        pm._last_spawn_config = None

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_monitor_desired_state_uses_last_config(self, setup):
        pm, manager = setup
        manager.desired_agents = 1
        pm._last_spawn_config = "/fallback.yaml"

        with patch.object(pm, "_launch_queued_agent", new_callable=AsyncMock):
            task = asyncio.create_task(pm.monitor_processes())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_monitor_keeps_existing_exit_reason_on_success(self, setup):
        """arc 341->356: exit_reason already set → if not agent.exit_reason: False."""
        pm, manager = setup
        proc = MagicMock()
        proc.poll.side_effect = [0, None]
        proc.returncode = 0
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(
            agent_id="agent_000",
            state="running",
            exit_reason="pre_existing_reason",
        )

        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager.agents["agent_000"].exit_reason == "pre_existing_reason"


class TestProcessSpawnEdgeCases:
    """Cover process.py lines 102, 176, 180 and branch arcs 84->82, 296->299."""

    def test_allocate_skips_processes_too(self, pm, manager):
        """Line 102: idx += 1 when candidate is in processes."""
        proc = MagicMock()
        manager.processes["agent_000"] = proc
        bid = pm.allocate_agent_id()
        assert bid == "agent_001"

    def test_sync_skips_non_agent_ids(self, pm, manager):
        """arc 84->82: _parse_agent_index returns None for non-numeric IDs."""
        manager.agents["custom-id"] = AgentStatusBase(agent_id="custom-id")
        idx = pm.sync_next_agent_index()
        assert idx >= 0

    @pytest.mark.asyncio
    async def test_kill_cleanup_handles_missing_agent(self, pm, manager):
        """arc 296->299: agent_id not in agents during kill_agent cleanup."""
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=None)
        proc.poll.return_value = None
        manager.processes["ghost"] = proc
        manager.broadcast_status = AsyncMock()
        await pm.kill_agent("ghost")
        assert "ghost" not in manager.processes

    @pytest.mark.asyncio
    async def test_spawn_agent_passes_name_base(self, pm, manager, tmp_path):
        """env[NAME_BASE] set when _spawn_name_base is not empty."""
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        pm._spawn_name_base = "fleet"
        manager.broadcast_status = AsyncMock()

        captured_env = {}

        def capture_spawn(agent_id, cmd, env):
            captured_env.update(env)
            return MagicMock(pid=456)

        with patch.object(pm, "_spawn_process", side_effect=capture_spawn):
            await pm.spawn_agent(str(config), "agent_000")
        assert captured_env.get("UTERM_NAME_BASE") == "fleet"
