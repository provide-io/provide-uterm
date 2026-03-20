# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for provide.terminal.manager.process."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager.config import ManagerConfig
from provide.terminal.manager.core import SwarmManager
from provide.terminal.manager.models import BotStatusBase
from provide.terminal.manager.process import BotProcessManager


class FakeWorkerPlugin:
    """Minimal WorkerRegistryPlugin implementation for tests."""

    @property
    def game_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_worker_module"

    def configure_worker_env(self, env, bot_status, manager):
        env["TEST_CUSTOM"] = "value"


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
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


class TestBotIdManagement:
    def test_parse_bot_index(self):
        assert BotProcessManager._parse_bot_index("bot_000") == 0
        assert BotProcessManager._parse_bot_index("bot_042") == 42
        assert BotProcessManager._parse_bot_index("invalid") is None
        assert BotProcessManager._parse_bot_index("") is None
        assert BotProcessManager._parse_bot_index("bot_") is None

    def test_allocate_bot_id(self, pm, manager):
        bid = pm.allocate_bot_id()
        assert bid == "bot_000"
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000")
        bid2 = pm.allocate_bot_id()
        assert bid2 == "bot_001"

    def test_sync_next_bot_index(self, pm, manager):
        manager.bots["bot_005"] = BotStatusBase(bot_id="bot_005")
        manager.bots["bot_010"] = BotStatusBase(bot_id="bot_010")
        idx = pm.sync_next_bot_index()
        assert idx == 11

    def test_note_bot_id(self, pm):
        pm.note_bot_id("bot_050")
        assert pm._next_bot_index == 51
        pm.note_bot_id("invalid_id")
        assert pm._next_bot_index == 51  # unchanged

    def test_allocate_skips_existing(self, pm, manager):
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001")
        bid = pm.allocate_bot_id()
        assert bid == "bot_002"


class TestCancelSpawn:
    @pytest.mark.asyncio
    async def test_cancel_empty(self, pm):
        assert await pm.cancel_spawn() is False

    @pytest.mark.asyncio
    async def test_cancel_with_tasks(self, pm):
        import asyncio

        async def noop():
            await asyncio.sleep(100)

        task = asyncio.create_task(noop())
        pm._spawn_tasks = [task]
        assert await pm.cancel_spawn() is True
        assert task.cancelled()


class TestReleaseAccount:
    def test_no_pool(self, pm, manager):
        manager.account_pool = None
        pm.release_bot_account("bot_000")  # should not raise

    def test_with_pool(self, pm, manager):
        pool = MagicMock()
        pool.release_by_bot.return_value = True
        manager.account_pool = pool
        pm.release_bot_account("bot_000")
        pool.release_by_bot.assert_called_once_with(bot_id="bot_000", cooldown_s=0)

    def test_pool_error(self, pm, manager):
        pool = MagicMock()
        pool.release_by_bot.side_effect = RuntimeError("fail")
        manager.account_pool = pool
        pm.release_bot_account("bot_000")  # should not raise


class TestSpawnBot:
    @pytest.mark.asyncio
    async def test_max_bots_reached(self, pm, manager):
        manager.max_bots = 0
        with pytest.raises(RuntimeError, match="Max bots"):
            await pm.spawn_bot("/config.yaml", "bot_000")

    @pytest.mark.asyncio
    async def test_config_not_found(self, pm):
        with pytest.raises(RuntimeError, match="Config not found"):
            await pm.spawn_bot("/nonexistent.yaml", "bot_000")

    @pytest.mark.asyncio
    async def test_unknown_game_type(self, pm, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("game_type: unknown_game\n")
        with pytest.raises(RuntimeError, match="Unknown game_type"):
            await pm.spawn_bot(str(config), "bot_000")

    @pytest.mark.asyncio
    async def test_spawn_success(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("game_type: test_game\nconnection:\n  game_letter: B\n")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            result = await pm.spawn_bot(str(config), "bot_000")

        assert result == "bot_000"
        assert "bot_000" in manager.bots
        assert manager.bots["bot_000"].pid == 12345
        assert manager.bots["bot_000"].state == "running"

    @pytest.mark.asyncio
    async def test_spawn_updates_existing_bot(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("game_type: test_game\n")
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            await pm.spawn_bot(str(config), "bot_000")

        assert manager.bots["bot_000"].pid == 9999
        assert manager.bots["bot_000"].state == "running"

    @pytest.mark.asyncio
    async def test_spawn_process_failure(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("game_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with (
            patch.object(pm, "_spawn_process", side_effect=OSError("no such file")),
            pytest.raises(RuntimeError, match="Failed to spawn"),
        ):
            await pm.spawn_bot(str(config), "bot_000")

    @pytest.mark.asyncio
    async def test_spawn_bad_yaml(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("{{invalid yaml")
        # Should default to game_type "default" which isn't registered
        with pytest.raises(RuntimeError, match="Unknown game_type"):
            await pm.spawn_bot(str(config), "bot_000")


class TestKillBot:
    @pytest.mark.asyncio
    async def test_kill_unknown(self, pm, manager):
        manager.broadcast_status = AsyncMock()
        await pm.kill_bot("nonexistent")
        # Should not raise

    @pytest.mark.asyncio
    async def test_kill_terminates(self, pm, manager):
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        manager.processes["bot_000"] = mock_proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_bot("bot_000")
        mock_proc.terminate.assert_called_once()
        assert manager.bots["bot_000"].state == "stopped"
        assert "bot_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_kill_force_on_timeout(self, pm, manager):
        import asyncio

        mock_proc = MagicMock()

        # wait() blocks forever
        async def slow_wait():
            await asyncio.sleep(100)

        mock_proc.wait.side_effect = lambda: slow_wait()
        manager.processes["bot_000"] = mock_proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        # The wait_for will timeout, triggering kill
        with patch("asyncio.wait_for", side_effect=TimeoutError):
            await pm.kill_bot("bot_000")
        mock_proc.kill.assert_called_once()
