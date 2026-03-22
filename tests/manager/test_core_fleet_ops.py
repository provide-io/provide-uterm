#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for SwarmManager fleet operations (kill_all, clear_swarm, prune_dead, pause/resume)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# pause_swarm / resume_swarm
# ---------------------------------------------------------------------------


class TestPauseSwarm:
    @pytest.mark.asyncio
    async def test_pause_sets_flag_and_pauses_active_bots(self, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="stopped")
        manager.bots["bot_002"] = BotStatusBase(bot_id="bot_002", state="recovering")

        result = await manager.pause_swarm()

        assert manager.swarm_paused is True
        assert manager.bots["bot_000"].paused is True
        assert manager.bots["bot_001"].paused is False  # stopped — not paused
        assert manager.bots["bot_002"].paused is True
        assert result["paused"] is True
        assert result["affected"] == 2
        manager.broadcast_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pause_with_no_bots(self, manager, pm):
        result = await manager.pause_swarm()
        assert result["affected"] == 0
        assert manager.swarm_paused is True


class TestResumeSwarm:
    @pytest.mark.asyncio
    async def test_resume_unsets_flag_and_unpauses_bots(self, manager, pm):
        manager.swarm_paused = True
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_000"].paused = True
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="running")
        manager.bots["bot_001"].paused = True

        result = await manager.resume_swarm()

        assert manager.swarm_paused is False
        assert manager.bots["bot_000"].paused is False
        assert manager.bots["bot_001"].paused is False
        assert result["resumed"] == 2
        assert result["paused"] is False
        manager.broadcast_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_with_no_paused_bots(self, manager, pm):
        manager.swarm_paused = True
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
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
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", pid=42)
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="running", pid=43)
        manager.processes["bot_000"] = proc_mock
        manager.processes["bot_001"] = proc_mock

        killed_ids = []

        async def tracking_kill(bot_id):
            killed_ids.append(bot_id)
            # Simulate kill_bot removing from processes
            manager.processes.pop(bot_id, None)

        manager.kill_bot = tracking_kill
        manager.cancel_spawn = AsyncMock(return_value=False)

        result = await manager.kill_all()

        assert set(result["killed"]) == {"bot_000", "bot_001"}
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
    async def test_clear_removes_all_bots(self, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="stopped")
        manager.cancel_spawn = AsyncMock(return_value=False)
        manager.kill_bot = AsyncMock()

        result = await manager.clear_swarm()

        assert result["cleared"] == 2
        assert len(manager.bots) == 0
        assert len(manager.processes) == 0
        manager.broadcast_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# prune_dead
# ---------------------------------------------------------------------------


class TestPruneDead:
    @pytest.mark.asyncio
    async def test_prune_removes_terminal_bots(self, manager, pm):
        manager.bots["bot_running"] = BotStatusBase(bot_id="bot_running", state="running")
        manager.bots["bot_stopped"] = BotStatusBase(bot_id="bot_stopped", state="stopped")
        manager.bots["bot_error"] = BotStatusBase(bot_id="bot_error", state="error")
        manager.bots["bot_done"] = BotStatusBase(bot_id="bot_done", state="completed")

        result = await manager.prune_dead()

        assert result["pruned"] == 3
        assert result["remaining"] == 1
        assert "bot_running" in manager.bots
        assert "bot_stopped" not in manager.bots
        assert "bot_error" not in manager.bots
        assert "bot_done" not in manager.bots
        manager.broadcast_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prune_with_no_dead_bots(self, manager, pm):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        result = await manager.prune_dead()
        assert result["pruned"] == 0
        assert result["remaining"] == 1
