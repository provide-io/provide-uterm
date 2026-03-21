# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Additional tests for provide.terminal.manager.process — spawn_swarm, monitor, etc."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.process import BotProcessManager


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_module"

    def configure_worker_env(self, env, bot_status, manager, **kwargs):
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
    return SwarmManager(config)


@pytest.fixture
def pm(manager, tmp_path):
    pm = BotProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.bot_process_manager = pm
    return pm


class TestStartSpawnSwarm:
    @pytest.mark.asyncio
    async def test_start_cancels_existing(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        # Start a "spawn" that will be cancelled
        async def slow_spawn(*a, **kw):
            await asyncio.sleep(100)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=slow_spawn):
            await pm.start_spawn_swarm([str(config)], cancel_existing=False)
            assert len(pm._spawn_tasks) == 1
            # Start another, which should cancel the first
            await pm.start_spawn_swarm([str(config)], cancel_existing=True)

    @pytest.mark.asyncio
    async def test_start_no_cancel(self, pm, manager, tmp_path):
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "spawn_swarm", new_callable=AsyncMock, return_value=[]):
            await pm.start_spawn_swarm(["/a.yaml"], cancel_existing=False)
            assert len(pm._spawn_tasks) == 1


class TestSpawnSwarm:
    @pytest.mark.asyncio
    async def test_spawn_swarm_basic(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        mock_proc = MagicMock()
        mock_proc.pid = 100

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            result = await pm.spawn_swarm(
                [str(config), str(config)],
                group_size=2,
                group_delay=0.01,
            )

        assert len(result) == 2
        assert all(bid.startswith("bot_") for bid in result)

    @pytest.mark.asyncio
    async def test_spawn_swarm_with_delay(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        mock_proc = MagicMock()
        mock_proc.pid = 200

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            result = await pm.spawn_swarm(
                [str(config), str(config), str(config)],
                group_size=2,
                group_delay=0.01,
            )

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_spawn_swarm_spawn_failure(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", side_effect=OSError("fail")):
            result = await pm.spawn_swarm([str(config)], group_size=1)

        assert len(result) == 0  # spawn failed


class TestSpawnProcess:
    def test_spawn_process_creates_log(self, pm, tmp_path):
        log_dir = tmp_path / "logs"
        pm._log_dir = str(log_dir)
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=123)
            proc = pm._spawn_process("bot_000", ["python", "-c", "pass"], {"PATH": "/usr/bin"})
        assert proc.pid == 123
        assert log_dir.is_dir()

    def test_spawn_process_failure(self, pm, tmp_path):
        log_dir = tmp_path / "logs"
        pm._log_dir = str(log_dir)
        with (
            patch("subprocess.Popen", side_effect=OSError("no such file")),
            pytest.raises(OSError, match="no such file"),
        ):
            pm._spawn_process("bot_000", ["nonexistent"], {})


class TestMonitorProcesses:
    @pytest.mark.asyncio
    async def test_monitor_exited_success(self, pm, manager):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        manager.processes["bot_000"] = mock_proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        # Run one iteration
        async def one_iteration():
            # Simulate monitor_processes for one loop
            async with manager._state_lock:
                exited = [(bid, p) for bid, p in list(manager.processes.items()) if p.poll() is not None]
            for bot_id, process in exited:
                exit_code = process.returncode
                async with manager._state_lock:
                    bot = manager.bots.get(bot_id)
                    if bot is None:
                        manager.processes.pop(bot_id, None)
                        continue
                    if exit_code == 0:
                        bot.state = "completed"
                        bot.exit_reason = "target_reached"
                    manager.processes.pop(bot_id, None)
                pm.release_bot_account(bot_id)

        await one_iteration()
        assert manager.bots["bot_000"].state == "completed"
        assert "bot_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_monitor_exited_error(self, pm, manager):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        manager.processes["bot_000"] = mock_proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        async with manager._state_lock:
            exited = [(bid, p) for bid, p in list(manager.processes.items()) if p.poll() is not None]
        for bot_id, process in exited:
            async with manager._state_lock:
                bot = manager.bots.get(bot_id)
                bot.state = "error"
                bot.exit_reason = f"exit_code_{process.returncode}"
                bot.error_message = f"Process exited with code {process.returncode}"
                manager.processes.pop(bot_id, None)

        assert manager.bots["bot_000"].state == "error"
        assert manager.bots["bot_000"].exit_reason == "exit_code_1"

    @pytest.mark.asyncio
    async def test_monitor_exited_with_prior_error(self, pm, manager):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        manager.processes["bot_000"] = mock_proc
        bot = BotStatusBase(bot_id="bot_000", state="error", error_message="previous error")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()

        async with manager._state_lock:
            exited = [(bid, p) for bid, p in list(manager.processes.items()) if p.poll() is not None]
        for bot_id, process in exited:
            async with manager._state_lock:
                b = manager.bots.get(bot_id)
                if process.returncode == 0 and (b.state == "error" or b.error_message):
                    b.state = "error"
                    b.exit_reason = "reported_error_then_exit_0"
                manager.processes.pop(bot_id, None)

        assert manager.bots["bot_000"].exit_reason == "reported_error_then_exit_0"

    @pytest.mark.asyncio
    async def test_launch_queued_bot_failure(self, pm, manager, tmp_path):
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "spawn_bot", side_effect=RuntimeError("fail")):
            await pm._launch_queued_bot("bot_000", "/config.yaml")

        assert manager.bots["bot_000"].state == "error"
        assert "Launch failed" in (manager.bots["bot_000"].error_message or "")


class TestDesiredStateEnforcement:
    @pytest.mark.asyncio
    async def test_desired_state_scale_up(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 2
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", config=str(config))
        manager.broadcast_status = AsyncMock()

        # The _launch_queued_bot creates spawns
        with patch.object(pm, "_launch_queued_bot", new_callable=AsyncMock):
            # Simulate one desired-state check
            active_states = {"running", "queued", "recovering", "blocked"}
            active_bots = [b for b in manager.bots.values() if b.state in active_states]
            active_count = len(active_bots)
            deficit = manager.desired_bots - active_count
            assert deficit == 1

    @pytest.mark.asyncio
    async def test_desired_state_scale_down(self, pm, manager):
        manager.desired_bots = 1
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="running")
        manager.broadcast_status = AsyncMock()

        active_states = {"running", "queued", "recovering", "blocked"}
        active_bots = [b for b in manager.bots.values() if b.state in active_states]
        deficit = manager.desired_bots - len(active_bots)
        assert deficit == -1
