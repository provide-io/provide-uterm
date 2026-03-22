#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.terminal.manager.process — supplemental batch (part 2).

Classes: TestSpawnBotStateUpdate, TestSpawnBotErrorPath, TestSpawnSwarmExtra.
See test_process_mutation_killing_3.py for: TestSpawnProcessExtra, TestKillBotExtra,
TestReleaseBotAccountExtra, TestLaunchQueuedBotExtra.
"""

from __future__ import annotations

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
# spawn_bot — state update block for existing bots (mutmut_116-118, 132-133)
# ---------------------------------------------------------------------------
class TestSpawnBotStateUpdate:
    @pytest.mark.asyncio
    async def test_last_update_time_set_not_none(self, pm, manager, tmp_path):
        """mutmut_116: last_update_time = None."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert manager.bots["bot_000"].last_update_time is not None
        assert manager.bots["bot_000"].last_update_time > 0

    @pytest.mark.asyncio
    async def test_started_at_set_not_none(self, pm, manager, tmp_path):
        """mutmut_117: started_at = None for existing bot branch."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert manager.bots["bot_000"].started_at is not None

    @pytest.mark.asyncio
    async def test_stopped_at_set_to_none_not_empty_string(self, pm, manager, tmp_path):
        """mutmut_118: stopped_at = '' instead of None."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0, stopped_at=9999.0)

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert manager.bots["bot_000"].stopped_at is None

    @pytest.mark.asyncio
    async def test_process_stored_in_processes_not_none(self, pm, manager, tmp_path):
        """mutmut_132: processes[bot_id] = None."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        proc = make_mock_proc(pid=99)
        with patch.object(pm, "_spawn_process", return_value=proc):
            await pm.spawn_bot(str(config), "bot_000")

        assert "bot_000" in manager.processes
        assert manager.processes["bot_000"] is not None
        assert manager.processes["bot_000"] is proc

    @pytest.mark.asyncio
    async def test_last_spawn_config_set_after_spawn(self, pm, manager, tmp_path):
        """mutmut_133: _last_spawn_config = None."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert pm._last_spawn_config == str(config)

    @pytest.mark.asyncio
    async def test_new_bot_config_set_in_else_branch(self, pm, manager, tmp_path):
        """mutmut_127: config=config_path omitted from _bot_status_class call in else branch."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert "bot_000" in manager.bots
        assert manager.bots["bot_000"].config == str(config)

    @pytest.mark.asyncio
    async def test_new_bot_started_at_set_in_else_branch(self, pm, manager, tmp_path):
        """mutmut_129: started_at=None in else branch."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert manager.bots["bot_000"].started_at is not None

    @pytest.mark.asyncio
    async def test_new_bot_state_is_running(self, pm, manager, tmp_path):
        """mutmut_122 (config=None), verify state='running' in else branch."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_bot(str(config), "bot_000")

        assert manager.bots["bot_000"].state == "running"

    @pytest.mark.asyncio
    async def test_return_value_is_bot_id(self, pm, manager, tmp_path):
        """spawn_bot must return the bot_id string."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_bot(str(config), "bot_007")

        assert result == "bot_007"


# ---------------------------------------------------------------------------
# spawn_bot — error path log messages (mutmut_142-150)
# ---------------------------------------------------------------------------
class TestSpawnBotErrorPath:
    @pytest.mark.asyncio
    async def test_spawn_failure_raises_runtime_error(self, pm, manager, tmp_path):
        """mutmut_142-150: logger.exception arg mutations — still raises."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")

        with (
            patch.object(pm, "_spawn_process", side_effect=OSError("boom")),
            pytest.raises(RuntimeError, match="Failed to spawn bot"),
        ):
            await pm.spawn_bot(str(config), "bot_000")

    @pytest.mark.asyncio
    async def test_spawn_failure_error_message_contains_original(self, pm, manager, tmp_path):
        """mutmut_144: error=None would lose original error message."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")

        with (
            patch.object(pm, "_spawn_process", side_effect=OSError("specific_error_xyz")),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await pm.spawn_bot(str(config), "bot_000")

        assert "specific_error_xyz" in str(exc_info.value)


# ---------------------------------------------------------------------------
# spawn_swarm — surviving mutants covering state updates and flow
# ---------------------------------------------------------------------------
class TestSpawnSwarmExtra:
    @pytest.mark.asyncio
    async def test_name_style_stored_as_passed(self, pm, manager, tmp_path):
        """mutmut_1-7: _spawn_name_style/base stored correctly."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_bot", new_callable=AsyncMock, return_value="bot_000"):
            await pm.spawn_swarm([str(config)], name_style="prefix", name_base="mybot")

        assert pm._spawn_name_style == "prefix"
        assert pm._spawn_name_base == "mybot"

    @pytest.mark.asyncio
    async def test_pre_registers_bots_as_queued(self, pm, manager, tmp_path):
        """mutmut_10/11: pre-registration of bots as 'queued'."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        captured_before_spawn = {}
        orig_spawn = pm.spawn_bot
        called = [False]

        async def spy(cfg, bid):
            if not called[0]:
                called[0] = True
                for k, v in manager.bots.items():
                    captured_before_spawn[k] = v.state
            return await orig_spawn(cfg, bid)

        with (
            patch.object(pm, "spawn_bot", side_effect=spy),
            patch.object(pm, "_spawn_process", return_value=make_mock_proc()),
        ):
            await pm.spawn_swarm([str(config), str(config)], group_size=2)

        assert len(captured_before_spawn) >= 2
        for state in captured_before_spawn.values():
            assert state == "queued"

    @pytest.mark.asyncio
    async def test_next_bot_index_advanced_by_total(self, pm, manager, tmp_path):
        """mutmut_14/17: base_index or _next_bot_index mutations."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_bot", new_callable=AsyncMock, return_value="bot_000"):
            await pm.spawn_swarm([str(config), str(config), str(config)])

        assert pm._next_bot_index >= 3

    @pytest.mark.asyncio
    async def test_bot_ids_use_base_index_format(self, pm, manager, tmp_path):
        """mutmut_21/22: bot_id = f'bot_{base_index + i:03d}' format."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        spawned = []

        async def spy(cfg, bid):
            spawned.append(bid)
            return bid

        with patch.object(pm, "spawn_bot", side_effect=spy):
            await pm.spawn_swarm([str(config), str(config)], group_size=2)

        import re

        for bid in spawned:
            assert re.match(r"^bot_\d{3}$", bid)

    @pytest.mark.asyncio
    async def test_group_end_uses_min(self, pm, manager, tmp_path):
        """mutmut_44/45: group_end = min(group_start + group_size, total)."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_swarm([str(config)] * 5, group_size=3, group_delay=0.0)

        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_group_configs_sliced_correctly(self, pm, manager, tmp_path):
        """mutmut_49-55: config_paths[group_start:group_end] slice."""
        config_a = tmp_path / "a.yaml"
        config_b = tmp_path / "b.yaml"
        config_c = tmp_path / "c.yaml"
        for c in [config_a, config_b, config_c]:
            c.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        spawned_configs = []
        orig_spawn = pm.spawn_bot

        async def spy(cfg, bid):
            spawned_configs.append(cfg)
            return await orig_spawn(cfg, bid)

        with (
            patch.object(pm, "spawn_bot", side_effect=spy),
            patch.object(pm, "_spawn_process", return_value=make_mock_proc()),
        ):
            await pm.spawn_swarm(
                [str(config_a), str(config_b), str(config_c)],
                group_size=2,
                group_delay=0.0,
            )

        assert str(config_a) in spawned_configs
        assert str(config_b) in spawned_configs
        assert str(config_c) in spawned_configs

    @pytest.mark.asyncio
    async def test_returns_spawned_bot_ids(self, pm, manager, tmp_path):
        """mutmut_60-74: return value / bot_ids list mutations."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_swarm([str(config), str(config)], group_size=2)

        assert len(result) == 2
        for bid in result:
            import re

            assert re.match(r"^bot_\d{3}$", bid)

    @pytest.mark.asyncio
    async def test_sleep_between_groups(self, pm, manager, tmp_path):
        """mutmut_92-95: group_end < total -> sleep; verify not when last group."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        sleep_calls = []

        async def fake_sleep(t):
            sleep_calls.append(t)

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(pm, "_spawn_process", return_value=make_mock_proc()),
        ):
            await pm.spawn_swarm([str(config)] * 4, group_size=2, group_delay=7.0)

        assert len(sleep_calls) >= 1
        assert 7.0 in sleep_calls

    @pytest.mark.asyncio
    async def test_no_sleep_for_single_group(self, pm, manager, tmp_path):
        """mutmut_97: sleep even when group_end == total."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        sleep_calls = []

        async def fake_sleep(t):
            sleep_calls.append(t)

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(pm, "_spawn_process", return_value=make_mock_proc()),
        ):
            await pm.spawn_swarm([str(config)], group_size=5, group_delay=99.0)

        assert 99.0 not in sleep_calls

    @pytest.mark.asyncio
    async def test_broadcast_called_after_swarm(self, pm, manager, tmp_path):
        """mutmut_100-112: broadcast_status calls."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_swarm([str(config)])

        manager.broadcast_status.assert_called()
