#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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
    async def test_bust_respawn_enabled_running_bot_with_bust_context(self, pm, manager):
        """mutmut_139-142: bust_respawn and not swarm_paused checks."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        bot = BotStatusBase(bot_id="bot_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(bot, "activity_context", "BUST")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "stopped"
        assert manager.bots["bot_000"].exit_reason == "bust_respawn"

    @pytest.mark.asyncio
    async def test_bust_respawn_skips_when_paused(self, pm, manager):
        """mutmut_140: bust_respawn fires even when swarm_paused -> must not."""
        manager.bust_respawn = True
        manager.swarm_paused = True
        bot = BotStatusBase(bot_id="bot_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(bot, "activity_context", "BUST")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "running"

    @pytest.mark.asyncio
    async def test_bust_respawn_only_kills_running_bots(self, pm, manager):
        """mutmut_150: state != 'running' check."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        bot = BotStatusBase(bot_id="bot_000", state="queued", pid=0)
        object.__setattr__(bot, "activity_context", "BUST")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "queued"

    @pytest.mark.asyncio
    async def test_bust_respawn_skips_non_bust_context(self, pm, manager):
        """mutmut_156: ctx != 'BUST' check."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        bot = BotStatusBase(bot_id="bot_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(bot, "activity_context", "PLAYING")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "running"

    @pytest.mark.asyncio
    async def test_bust_respawn_context_comparison_is_uppercase(self, pm, manager):
        """mutmut_163: 'BUST' comparison (lowercase bust should work too via .upper())."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        bot = BotStatusBase(bot_id="bot_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(bot, "activity_context", "bust")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "stopped"

    @pytest.mark.asyncio
    async def test_bust_exit_reason_is_bust_respawn(self, pm, manager):
        """mutmut_166: exit_reason='bust_respawn'."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        bot = BotStatusBase(bot_id="bot_000", state="running", pid=0, last_update_time=time.time())
        object.__setattr__(bot, "activity_context", "BUST")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].exit_reason == "bust_respawn"

    @pytest.mark.asyncio
    async def test_bust_stopped_at_set(self, pm, manager):
        """mutmut_169/170: stopped_at = now."""
        manager.bust_respawn = True
        manager.swarm_paused = False
        bot = BotStatusBase(bot_id="bot_000", state="running", pid=0)
        object.__setattr__(bot, "activity_context", "BUST")
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        t_before = time.time()
        await self._run_one(pm, manager)
        t_after = time.time()

        assert manager.bots["bot_000"].stopped_at is not None
        assert t_before <= manager.bots["bot_000"].stopped_at <= t_after + 0.1


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
    async def test_desired_bots_zero_skips_enforcement(self, pm, manager):
        """mutmut_187: desired_bots > 0 check."""
        manager.desired_bots = 0
        manager.swarm_paused = False
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="error")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_bot", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert len(launched) == 0

    @pytest.mark.asyncio
    async def test_desired_state_skips_when_paused(self, pm, manager, tmp_path):
        """mutmut_189: not swarm_paused check."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 2
        manager.swarm_paused = True
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_bot", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert len(launched) == 0

    @pytest.mark.asyncio
    async def test_dead_bots_pruned_from_bots(self, pm, manager, tmp_path):
        """mutmut_190-202: dead_bots pruning logic."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 1
        manager.swarm_paused = False
        manager.bots["bot_error"] = BotStatusBase(bot_id="bot_error", state="error", config=str(config))
        manager.bots["bot_running"] = BotStatusBase(bot_id="bot_running", state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_bot", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert "bot_error" not in manager.bots

    @pytest.mark.asyncio
    async def test_active_states_set(self, pm, manager, tmp_path):
        """mutmut_208: active_states = {'running', 'queued', 'recovering', 'blocked'}."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 3
        manager.swarm_paused = False
        for bid in ["bot_000", "bot_001"]:
            manager.bots[bid] = BotStatusBase(bot_id=bid, state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def fake_launch(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_bot", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert len(launched) >= 1

    @pytest.mark.asyncio
    async def test_deficit_positive_spawns_bots(self, pm, manager, tmp_path):
        """mutmut_216-228: deficit > 0 branch spawns correct count."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 3
        manager.swarm_paused = False
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000", state="running", config=str(config), last_update_time=time.time()
        )
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def fake_launch(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_bot", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert len(launched) == 2

    @pytest.mark.asyncio
    async def test_configs_from_active_bots(self, pm, manager, tmp_path):
        """mutmut_225-235: configs_available from active_bots first."""
        config_a = tmp_path / "a.yaml"
        config_a.write_text("worker_type: test_game\n")
        manager.desired_bots = 2
        manager.swarm_paused = False
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000", state="running", config=str(config_a), last_update_time=time.time()
        )
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched_with_config = []

        async def fake_launch(bid, cfg):
            launched_with_config.append((bid, cfg))

        with patch.object(pm, "_launch_queued_bot", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert len(launched_with_config) == 1
        assert launched_with_config[0][1] == str(config_a)

    @pytest.mark.asyncio
    async def test_last_spawn_config_fallback(self, pm, manager, tmp_path):
        """mutmut_237-241: _last_spawn_config fallback when no active/dead configs."""
        config = tmp_path / "last.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 1
        manager.swarm_paused = False
        pm._last_spawn_config = str(config)
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched_with_config = []

        async def fake_launch(bid, cfg):
            launched_with_config.append(cfg)

        with patch.object(pm, "_launch_queued_bot", side_effect=fake_launch):
            await self._run_one(pm, manager)

        assert str(config) in launched_with_config

    @pytest.mark.asyncio
    async def test_queued_status_created_for_new_bot(self, pm, manager, tmp_path):
        """mutmut_243-259: new bot created with state='queued'."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 2
        manager.swarm_paused = False
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def fake_launch(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_bot", side_effect=fake_launch):
            await self._run_one(pm, manager)

        for bid in launched:
            if bid in manager.bots:
                assert manager.bots[bid].state in ("queued", "running")

    @pytest.mark.asyncio
    async def test_excess_bots_killed_when_over_desired(self, pm, manager):
        """mutmut_266-334: deficit < 0 -> kill excess bots."""
        manager.desired_bots = 1
        manager.swarm_paused = False
        for bid in ["bot_002", "bot_001", "bot_000"]:
            manager.bots[bid] = BotStatusBase(bot_id=bid, state="running", pid=0, last_update_time=time.time())
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        kill_calls = []

        async def fake_kill(bid):
            kill_calls.append(bid)

        with patch.object(manager, "kill_bot", side_effect=fake_kill):
            await self._run_one(pm, manager)

        assert len(kill_calls) == 2

    @pytest.mark.asyncio
    async def test_excess_sorted_descending_by_bot_id(self, pm, manager):
        """mutmut_291: reverse=True -> kills highest-id bots first."""
        manager.desired_bots = 1
        manager.swarm_paused = False
        for bid in ["bot_000", "bot_001", "bot_002"]:
            manager.bots[bid] = BotStatusBase(bot_id=bid, state="running", pid=0, last_update_time=time.time())
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        kill_calls = []

        async def fake_kill(bid):
            kill_calls.append(bid)

        with patch.object(manager, "kill_bot", side_effect=fake_kill):
            await self._run_one(pm, manager)

        assert "bot_002" in kill_calls
        assert "bot_001" in kill_calls
        assert "bot_000" not in kill_calls

    @pytest.mark.asyncio
    async def test_excess_bots_removed_from_bots(self, pm, manager):
        """mutmut_306-334: bots.pop and processes.pop called for excess."""
        manager.desired_bots = 1
        manager.swarm_paused = False
        for bid in ["bot_000", "bot_001", "bot_002"]:
            manager.bots[bid] = BotStatusBase(bot_id=bid, state="running", pid=0)
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        async def fake_kill(bid):
            pass

        with patch.object(manager, "kill_bot", side_effect=fake_kill):
            await self._run_one(pm, manager)

        remaining = len(manager.bots)
        assert remaining <= 2
