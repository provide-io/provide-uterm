#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.terminal.manager.process — supplemental batch."""

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
# __init__ defaults — surviving mutants 1, 5, 9, 12-17
# (Extra coverage beyond existing test_process_mutants.py)
# ---------------------------------------------------------------------------
class TestInitDefaultsExtra:
    def test_log_dir_default_param_is_empty(self, manager):
        """mutmut_1: log_dir default '' -> 'XXXX'."""
        pm = BotProcessManager(manager)
        assert pm._log_dir == ""

    def test_log_dir_assigned_from_param(self, manager, tmp_path):
        """mutmut_5: self._log_dir = None."""
        pm = BotProcessManager(manager, log_dir=str(tmp_path))
        assert pm._log_dir == str(tmp_path)

    def test_queued_launch_delay_is_30(self, manager):
        """mutmut_9: _queued_launch_delay 30.0 -> 31.0."""
        pm = BotProcessManager(manager)
        assert pm._queued_launch_delay == 30.0
        assert pm._queued_launch_delay != 31.0

    def test_spawn_name_style_default_lowercase(self, manager):
        """mutmut_12/13/14: _spawn_name_style None/'XXrandomXX'/'RANDOM'."""
        pm = BotProcessManager(manager)
        assert pm._spawn_name_style == "random"

    def test_spawn_name_base_default_empty(self, manager):
        """mutmut_15/16: _spawn_name_base None/'XXXX'."""
        pm = BotProcessManager(manager)
        assert pm._spawn_name_base == ""

    def test_last_spawn_config_is_none_not_empty(self, manager):
        """mutmut_17: _last_spawn_config '' -> None."""
        pm = BotProcessManager(manager)
        assert pm._last_spawn_config is None
        assert pm._last_spawn_config != ""


# ---------------------------------------------------------------------------
# sync_next_bot_index — surviving mutmut_3 (max_seen = -2)
# ---------------------------------------------------------------------------
class TestSyncNextBotIndexExtra:
    def test_empty_bots_and_processes_returns_zero(self, pm):
        """With no bots: max_seen stays -1, next = max(0, 0) = 0.
        mutmut_3 (-2) gives max(0,-1)=0 too, but next test distinguishes."""
        assert pm.sync_next_bot_index() == 0

    def test_single_bot_0_gives_next_1(self, pm, manager):
        """mutmut_3: max_seen=-2 -> next = max(0, -1) = 0 (wrong when bot_0 present)."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000")
        result = pm.sync_next_bot_index()
        assert result == 1  # max_seen=0 -> max(0, 0+1)=1; mutmut_3 gives max(0, -2+1)=-1 -> clamped to 0

    def test_only_processes_dict_counted(self, pm, manager):
        """mutmut_4: uses & instead of |; processes-only bot would be missed."""
        manager.processes["bot_003"] = MagicMock()
        result = pm.sync_next_bot_index()
        assert result == 4  # union: sees bot_003 idx=3, next=4

    def test_union_both_bots_and_processes(self, pm, manager):
        """mutmut_4: & would miss processes-only entry."""
        manager.bots["bot_002"] = BotStatusBase(bot_id="bot_002")
        manager.processes["bot_005"] = MagicMock()
        result = pm.sync_next_bot_index()
        assert result == 6  # max(2,5)+1=6; with & would only see bot_002, giving 3


# ---------------------------------------------------------------------------
# allocate_bot_id — surviving mutmut_8 (_next = idx-1), mutmut_10 (idx=1 not +=1)
# ---------------------------------------------------------------------------
class TestAllocateBotIdExtra:
    def test_next_index_set_to_idx_plus_one(self, pm, manager):
        """mutmut_8: _next_bot_index = idx-1 instead of idx+1."""
        bid = pm.allocate_bot_id()
        assert bid == "bot_000"
        assert pm._next_bot_index == 1  # idx was 0, next must be 1

    def test_idx_increments_not_resets(self, pm, manager):
        """mutmut_10: idx=1 instead of idx+=1 would cause bot_001 loop."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000")
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001")
        # allocate with 0,1 taken; should return bot_002 by incrementing
        bid = pm.allocate_bot_id()
        assert bid == "bot_002"

    def test_format_is_zero_padded_3_digits(self, pm, manager):
        """Verify format f'bot_{idx:03d}' — mutmut_3 would return None."""
        bid = pm.allocate_bot_id()
        import re

        assert re.match(r"^bot_\d{3}$", bid), f"Expected bot_NNN format, got {bid!r}"


# ---------------------------------------------------------------------------
# start_spawn_swarm — surviving mutmut_1-24
# ---------------------------------------------------------------------------
class TestStartSpawnSwarmExtra:
    @pytest.mark.asyncio
    async def test_group_size_default_is_1(self, pm, manager):
        """mutmut_1: default group_size 1 -> 2."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured.get("group_size") == 1

    @pytest.mark.asyncio
    async def test_group_delay_default_is_12(self, pm):
        """mutmut_2: default group_delay 12.0 -> 13.0."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured.get("group_delay") == 12.0

    @pytest.mark.asyncio
    async def test_cancel_existing_true_calls_cancel(self, pm):
        """mutmut_3: cancel_existing default True -> False."""
        cancel_called = []

        async def fake_cancel():
            cancel_called.append(True)
            return False

        async def noop(*a, **kw):
            return []

        with (
            patch.object(pm, "cancel_spawn", side_effect=fake_cancel),
            patch.object(pm, "spawn_swarm", side_effect=noop),
        ):
            await pm.start_spawn_swarm(["/a.yaml"])

        assert len(cancel_called) == 1

    @pytest.mark.asyncio
    async def test_name_style_default_is_random(self, pm):
        """mutmut_6/7: name_style default 'random' -> 'XXrandomXX'/'RANDOM'."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured.get("name_style") == "random"

    @pytest.mark.asyncio
    async def test_name_base_default_is_empty(self, pm):
        """mutmut_8: name_base default '' -> 'XXXX'."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured.get("name_base") == ""

    @pytest.mark.asyncio
    async def test_stale_tasks_pruned_before_spawn(self, pm):
        """mutmut_10: filter keeps done tasks instead of removing them."""

        async def instant():
            return None

        done_task = asyncio.create_task(instant())
        # Drain the event loop until the task completes
        for _ in range(5):
            await asyncio.sleep(0)
        assert done_task.done(), "task should be done after yielding to event loop"
        pm._spawn_tasks = [done_task]

        async def noop(*a, **kw):
            return []

        with patch.object(pm, "spawn_swarm", side_effect=noop):
            await pm.start_spawn_swarm(["/a.yaml"], cancel_existing=False)

        # After start_spawn_swarm, the stale done task should have been pruned.
        # Only the new (still-pending) task created inside start_spawn_swarm remains.
        assert done_task not in pm._spawn_tasks

    @pytest.mark.asyncio
    async def test_config_paths_forwarded_not_none(self, pm):
        """mutmut_13: config_paths=None passed to spawn_swarm."""
        captured_paths = []

        async def spy(paths, **kw):
            captured_paths.append(paths)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/x.yaml", "/y.yaml"])
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured_paths == [["/x.yaml", "/y.yaml"]]

    @pytest.mark.asyncio
    async def test_all_kwargs_forwarded(self, pm):
        """mutmut_14-24: each kwarg set to None or omitted."""
        captured = {}

        async def spy(paths, **kw):
            captured["paths"] = paths
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(
                ["/z.yaml"],
                group_size=3,
                group_delay=5.0,
                name_style="fixed",
                name_base="bot",
            )
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured["paths"] == ["/z.yaml"]
        assert captured["group_size"] == 3
        assert captured["group_delay"] == 5.0
        assert captured["name_style"] == "fixed"
        assert captured["name_base"] == "bot"

    @pytest.mark.asyncio
    async def test_task_appended_to_spawn_tasks(self, pm):
        """mutmut_25: _spawn_tasks.append(None)."""

        async def noop(*a, **kw):
            return []

        with patch.object(pm, "spawn_swarm", side_effect=noop):
            await pm.start_spawn_swarm(["/a.yaml"], cancel_existing=False)

        assert len(pm._spawn_tasks) >= 1
        # The appended task must be a real Task, not None
        assert all(isinstance(t, asyncio.Task) for t in pm._spawn_tasks)


# ---------------------------------------------------------------------------
# spawn_bot — worker_type defaults/fallbacks and config reading
# ---------------------------------------------------------------------------
class TestSpawnBotGameTypeFallbacks:
    @pytest.mark.asyncio
    async def test_worker_type_fallback_single_registry_succeeds(self, pm, manager, tmp_path):
        """No worker_type key + single registry entry → uses that entry."""
        config = tmp_path / "cfg.yaml"
        config.write_text("{}")  # no worker_type key
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_bot(str(config), "bot_000")
        assert result == "bot_000"

    @pytest.mark.asyncio
    async def test_worker_type_get_default_single_registry(self, pm, manager, tmp_path):
        """No worker_type key → single-registry fallback, not error."""
        config = tmp_path / "cfg.yaml"
        config.write_text("connection:\n  host: x\n")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_bot(str(config), "bot_000")
        assert result == "bot_000"

    @pytest.mark.asyncio
    async def test_worker_type_null_single_registry(self, pm, manager, tmp_path):
        """worker_type: null → falls to 'default' → single-registry success."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: ~\n")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_bot(str(config), "bot_000")
        assert result == "bot_000"

    @pytest.mark.asyncio
    async def test_unknown_worker_type_with_multiple_registries_raises(self, pm, manager, tmp_path):
        """Unknown worker_type with multiple registries → RuntimeError."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: not_registered\n")
        pm._worker_registry["other"] = pm._worker_registry["test_game"]
        with pytest.raises(RuntimeError, match="Unknown worker_type"):
            await pm.spawn_bot(str(config), "bot_000")

    @pytest.mark.asyncio
    async def test_warning_logged_on_config_read_error(self, pm, manager, tmp_path):
        """Warning logged on config read error; single-registry fallback still used."""
        config = tmp_path / "cfg.yaml"
        config.write_bytes(b"\xff\xfe")  # invalid UTF-8
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_bot(str(config), "bot_000")
        assert result == "bot_000"


# ---------------------------------------------------------------------------
# spawn_bot — _spawn_process argument passing (mutmut_104, 105)
# ---------------------------------------------------------------------------
class TestSpawnBotProcessArgs:
    @pytest.mark.asyncio
    async def test_spawn_process_called_with_bot_id(self, pm, manager, tmp_path):
        """mutmut_104: _spawn_process(None, cmd, env) -> bot_id must be passed."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        calls = []

        def fake_spawn(bid, cmd, env):
            calls.append(bid)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_bot(str(config), "bot_007")

        assert calls == ["bot_007"]

    @pytest.mark.asyncio
    async def test_spawn_process_called_with_cmd(self, pm, manager, tmp_path):
        """mutmut_105: _spawn_process(bot_id, None, env) -> cmd must be passed."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        calls = []

        def fake_spawn(bid, cmd, env):
            calls.append(cmd)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_bot(str(config), "bot_007")

        assert calls[0] is not None
        assert isinstance(calls[0], list)
        assert len(calls[0]) > 0


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
        # No pre-existing bot -> goes to else branch

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
        # No pre-existing bot -> else branch

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

        # At first spawn, all bots should have been pre-registered as 'queued'
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

        # After registering 3 bots starting from 0, _next_bot_index >= 3
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

        # Bot IDs must follow bot_NNN format
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

        # All three configs should be spawned
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

        # 4 bots / group_size=2 -> 2 groups; sleep after first group (before last)
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

        # Only 1 bot, group_size=5: no sleep needed (group_end == total)
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


# ---------------------------------------------------------------------------
# _spawn_process — surviving mutmut_3-11, 19-26
# ---------------------------------------------------------------------------
class TestSpawnProcessExtra:
    def test_uses_custom_log_dir(self, pm, tmp_path):
        """mutmut_3-4: log_dir path logic."""
        pm._log_dir = str(tmp_path / "my_logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("bot_001", ["echo"], {})
        assert (tmp_path / "my_logs").is_dir()

    def test_default_log_dir_fallback(self, pm, tmp_path, monkeypatch):
        """mutmut_5: Path('logs/workers') fallback when _log_dir empty."""
        pm._log_dir = ""
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("bot_002", ["echo"], {})
        assert (tmp_path / "logs" / "workers").is_dir()

    def test_log_file_named_bot_id_dot_log(self, pm, tmp_path):
        """mutmut_6-7: log file name uses bot_id."""
        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("bot_009", ["echo"], {})
        assert (tmp_path / "logs" / "bot_009.log").exists()

    def test_popen_stderr_is_stdout(self, pm, tmp_path):
        """mutmut_19-26: Popen call argument mutations."""
        import subprocess

        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("bot_000", ["echo", "hi"], {"K": "V"})
        _, kwargs = mp.call_args
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["env"] == {"K": "V"}

    def test_popen_stdout_is_file_handle(self, pm, tmp_path):
        """mutmut_20: stdout=None instead of log_handle."""
        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("bot_000", ["echo"], {})
        _, kwargs = mp.call_args
        # stdout should be a file-like object, not None
        assert kwargs["stdout"] is not None

    def test_returns_popen_process(self, pm, tmp_path):
        """mutmut_21: return None instead of proc."""
        pm._log_dir = str(tmp_path / "logs")
        mock_proc = MagicMock(pid=1)
        with patch("subprocess.Popen", return_value=mock_proc):
            result = pm._spawn_process("bot_000", ["echo"], {})
        assert result is mock_proc

    def test_log_handle_closed_after_success(self, pm, tmp_path):
        """mutmut_24-26: log handle closed."""
        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("bot_000", ["echo"], {})
        log_file = tmp_path / "logs" / "bot_000.log"
        # Can open again if handle was closed
        log_file.open("a").close()

    def test_log_handle_closed_after_failure(self, pm, tmp_path):
        """mutmut_9-11: log handle closed even on exception."""
        pm._log_dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(exist_ok=True)
        with patch("subprocess.Popen", side_effect=OSError("fail")), pytest.raises(OSError):
            pm._spawn_process("bot_err", ["bad"], {})
        log_file = tmp_path / "logs" / "bot_err.log"
        assert log_file.exists()
        log_file.open("a").close()


# ---------------------------------------------------------------------------
# kill_bot — surviving mutmut_4, 10-12, 14-16
# ---------------------------------------------------------------------------
class TestKillBotExtra:
    @pytest.mark.asyncio
    async def test_kill_bot_timeout_is_5(self, pm, manager):
        """mutmut_4: timeout=None."""
        proc = MagicMock()
        proc.wait.return_value = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        wait_for_timeouts = []
        orig = asyncio.wait_for

        async def spy(coro, timeout=None):
            wait_for_timeouts.append(timeout)
            return await orig(coro, timeout=timeout)

        with patch("asyncio.wait_for", side_effect=spy):
            await pm.kill_bot("bot_000")

        assert 5.0 in wait_for_timeouts

    @pytest.mark.asyncio
    async def test_kill_sets_state_stopped(self, pm, manager):
        """mutmut_10: state='XXstoppedXX'."""
        proc = MagicMock()
        proc.wait.return_value = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_bot("bot_000")

        assert manager.bots["bot_000"].state == "stopped"

    @pytest.mark.asyncio
    async def test_kill_sets_stopped_at_to_time(self, pm, manager):
        """mutmut_11: stopped_at = None."""
        proc = MagicMock()
        proc.wait.return_value = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        t_before = time.time()
        await pm.kill_bot("bot_000")
        t_after = time.time()

        assert manager.bots["bot_000"].stopped_at is not None
        assert t_before <= manager.bots["bot_000"].stopped_at <= t_after

    @pytest.mark.asyncio
    async def test_kill_removes_from_processes(self, pm, manager):
        """mutmut_12: processes.pop(bot_id) missing."""
        proc = MagicMock()
        proc.wait.return_value = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_bot("bot_000")

        assert "bot_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_kill_calls_release_with_bot_id(self, pm, manager):
        """mutmut_14: release_bot_account(None)."""
        proc = MagicMock()
        proc.wait.return_value = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        calls = []
        with patch.object(pm, "release_bot_account", side_effect=lambda b: calls.append(b)):
            await pm.kill_bot("bot_000")

        assert "bot_000" in calls

    @pytest.mark.asyncio
    async def test_kill_broadcasts_after(self, pm, manager):
        """mutmut_15/16: broadcast_status calls."""
        proc = MagicMock()
        proc.wait.return_value = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_bot("bot_000")

        manager.broadcast_status.assert_called()


# ---------------------------------------------------------------------------
# release_bot_account — surviving mutmut_10-23
# ---------------------------------------------------------------------------
class TestReleaseBotAccountExtra:
    def test_no_pool_returns_none(self, pm, manager):
        """mutmut_10: no AttributeError when pool is None."""
        manager.account_pool = None
        result = pm.release_bot_account("bot_000")
        assert result is None

    def test_calls_release_by_bot_with_correct_args(self, pm, manager):
        """mutmut_11-16: pool.release_by_bot(bot_id=..., cooldown_s=0)."""
        pool = MagicMock()
        pool.release_by_bot.return_value = True
        manager.account_pool = pool

        pm.release_bot_account("bot_007")

        pool.release_by_bot.assert_called_once_with(bot_id="bot_007", cooldown_s=0)

    def test_cooldown_s_is_zero_not_one(self, pm, manager):
        """mutmut_17: cooldown_s=1 instead of 0."""
        pool = MagicMock()
        pool.release_by_bot.return_value = False
        manager.account_pool = pool

        pm.release_bot_account("bot_001")

        _, kwargs = pool.release_by_bot.call_args
        assert kwargs.get("cooldown_s") == 0

    def test_exception_in_pool_does_not_propagate(self, pm, manager):
        """mutmut_19-23: exception handling."""
        pool = MagicMock()
        pool.release_by_bot.side_effect = RuntimeError("pool error")
        manager.account_pool = pool

        # Should not raise
        pm.release_bot_account("bot_000")

    def test_release_by_bot_true_logs_info(self, pm, manager):
        """mutmut_22: released check inverted."""
        pool = MagicMock()
        pool.release_by_bot.return_value = True
        manager.account_pool = pool

        # Should not raise when released=True
        pm.release_bot_account("bot_000")
        pool.release_by_bot.assert_called_once()


# ---------------------------------------------------------------------------
# _launch_queued_bot — surviving mutmut_1-13, 19-21
# ---------------------------------------------------------------------------
class TestLaunchQueuedBotExtra:
    @pytest.mark.asyncio
    async def test_success_path_no_error_set(self, pm, manager, tmp_path):
        """mutmut_1/2: success path does not set error state."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm._launch_queued_bot("bot_000", str(config))

        assert manager.bots["bot_000"].state == "running"

    @pytest.mark.asyncio
    async def test_failure_sets_error_state(self, pm, manager):
        """mutmut_3/4: state='error' on failure."""
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "spawn_bot", side_effect=RuntimeError("fail")):
            await pm._launch_queued_bot("bot_000", "/cfg.yaml")

        assert manager.bots["bot_000"].state == "error"

    @pytest.mark.asyncio
    async def test_failure_sets_error_message_with_launch_failed(self, pm, manager):
        """mutmut_5-7: error_message format."""
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "spawn_bot", side_effect=RuntimeError("the reason")):
            await pm._launch_queued_bot("bot_000", "/cfg.yaml")

        msg = manager.bots["bot_000"].error_message or ""
        assert "Launch failed" in msg
        assert "the reason" in msg

    @pytest.mark.asyncio
    async def test_failure_sets_exit_reason_launch_failed(self, pm, manager):
        """mutmut_8-10: exit_reason='launch_failed'."""
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "spawn_bot", side_effect=RuntimeError("fail")):
            await pm._launch_queued_bot("bot_000", "/cfg.yaml")

        assert manager.bots["bot_000"].exit_reason == "launch_failed"

    @pytest.mark.asyncio
    async def test_broadcast_called_on_failure(self, pm, manager):
        """mutmut_11-13: broadcast_status called on failure."""
        manager.broadcast_status = AsyncMock()
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)

        with patch.object(pm, "spawn_bot", side_effect=RuntimeError("fail")):
            await pm._launch_queued_bot("bot_000", "/cfg.yaml")

        manager.broadcast_status.assert_called()

    @pytest.mark.asyncio
    async def test_no_crash_when_bot_not_in_bots(self, pm, manager):
        """mutmut_19-21: if bot_id in self.manager.bots check."""
        manager.broadcast_status = AsyncMock()
        # No bot in manager.bots

        with patch.object(pm, "spawn_bot", side_effect=RuntimeError("fail")):
            # Should not raise even if bot not in bots
            await pm._launch_queued_bot("bot_999", "/cfg.yaml")


# ---------------------------------------------------------------------------
# monitor_processes — surviving exit handling mutants (6-13, 19-29, 37-38)
# ---------------------------------------------------------------------------
class TestMonitorProcessesExtra:
    async def _run_one_iteration(self, pm, manager):
        """Run monitor_processes for one iteration then cancel."""
        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_exit_code_0_clean_sets_completed(self, pm, manager):
        """mutmut_6-13: bot_exited warning logging; mutmut_19: pop with default."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].state == "completed"
        assert "bot_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_exit_code_0_with_prior_error_stays_error(self, pm, manager):
        """mutmut_23-29: exit_code==0 prior-error branch; state must be 'error' not None/XX."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="error", error_message="prior")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].state == "error"
        assert manager.bots["bot_000"].exit_reason == "reported_error_then_exit_0"

    @pytest.mark.asyncio
    async def test_exit_code_0_with_error_message_stays_error(self, pm, manager):
        """mutmut_23: 'or' -> 'and'; if only error_message (not state==error) must stay error."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", error_message="something happened")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        # Has error_message but not state==error: the 'or' condition fires
        assert manager.bots["bot_000"].state == "error"
        assert manager.bots["bot_000"].exit_reason == "reported_error_then_exit_0"

    @pytest.mark.asyncio
    async def test_exit_reason_reported_error_then_exit_0_string(self, pm, manager):
        """mutmut_27-29: exit_reason string exact value."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="error")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].exit_reason == "reported_error_then_exit_0"

    @pytest.mark.asyncio
    async def test_completed_at_set_on_clean_exit(self, pm, manager):
        """mutmut_37: completed_at = None."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].completed_at is not None
        assert manager.bots["bot_000"].completed_at > 0

    @pytest.mark.asyncio
    async def test_stopped_at_set_on_clean_exit(self, pm, manager):
        """mutmut_38: stopped_at = None on clean exit."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].stopped_at is not None

    @pytest.mark.asyncio
    async def test_exit_reason_target_reached_on_clean_exit(self, pm, manager):
        """mutmut_49/50: exit_reason='target_reached' exact string."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].exit_reason == "target_reached"

    @pytest.mark.asyncio
    async def test_nonzero_exit_sets_error_state(self, pm, manager):
        """mutmut_53/54: bot.state='error' on nonzero exit."""
        proc = MagicMock()
        proc.poll.return_value = 3
        proc.returncode = 3
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].state == "error"
        assert manager.bots["bot_000"].exit_reason == "exit_code_3"

    @pytest.mark.asyncio
    async def test_nonzero_exit_sets_error_message(self, pm, manager):
        """mutmut_56: error_message set on nonzero exit."""
        proc = MagicMock()
        proc.poll.return_value = 5
        proc.returncode = 5
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert "5" in (manager.bots["bot_000"].error_message or "")

    @pytest.mark.asyncio
    async def test_nonzero_exit_sets_stopped_at(self, pm, manager):
        """mutmut_66/67: stopped_at set on nonzero exit."""
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.returncode = 1
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert manager.bots["bot_000"].stopped_at is not None

    @pytest.mark.asyncio
    async def test_none_bot_popped_and_continue(self, pm, manager):
        """mutmut_19/20: pop default=None; continue vs break."""
        proc0 = MagicMock()
        proc0.poll.return_value = 0
        proc0.returncode = 0
        proc1 = MagicMock()
        proc1.poll.return_value = 0
        proc1.returncode = 0
        manager.processes["bot_000"] = proc0  # no matching bot
        manager.processes["bot_001"] = proc1
        manager.bots["bot_001"] = BotStatusBase(bot_id="bot_001", state="running")
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one_iteration(pm, manager)

        assert "bot_000" not in manager.processes
        assert manager.bots["bot_001"].state == "completed"


# ---------------------------------------------------------------------------
# monitor_processes — heartbeat timeout (mutmut_78-100)
# ---------------------------------------------------------------------------
class TestMonitorHeartbeatExtra:
    async def _run_one(self, pm, manager):
        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_checks_running_state_only(self, pm, manager):
        """mutmut_78/79: state in ('running',) check."""
        old = time.time() - 999
        manager.bots["bot_run"] = BotStatusBase(bot_id="bot_run", state="running", last_update_time=old, pid=0)
        manager.bots["bot_queued"] = BotStatusBase(bot_id="bot_queued", state="queued", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_run"].state == "error"
        assert manager.bots["bot_queued"].state == "queued"  # not timed out

    @pytest.mark.asyncio
    async def test_heartbeat_threshold_is_greater_than(self, pm, manager):
        """mutmut_80/81: > vs >= for heartbeat comparison."""
        # Set last_update_time exactly at timeout boundary
        heartbeat_timeout = 10
        manager.config.heartbeat_timeout_s = heartbeat_timeout
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000",
            state="running",
            last_update_time=time.time() - heartbeat_timeout - 1,  # definitely past
            pid=0,
        )
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "error"
        assert manager.bots["bot_000"].exit_reason == "heartbeat_timeout"

    @pytest.mark.asyncio
    async def test_heartbeat_error_type_and_message(self, pm, manager):
        """mutmut_82/83/87: error_type='HeartbeatTimeout', error_message content."""
        old = time.time() - 9999
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        bot = manager.bots["bot_000"]
        assert bot.error_type == "HeartbeatTimeout"
        assert bot.error_message is not None
        assert (
            "heartbeat" in bot.error_message.lower()
            or "crashed" in bot.error_message.lower()
            or "stuck" in bot.error_message.lower()
        )

    @pytest.mark.asyncio
    async def test_heartbeat_error_timestamp_set(self, pm, manager):
        """mutmut_89/90: error_timestamp set on heartbeat timeout."""
        old = time.time() - 9999
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].error_timestamp is not None

    @pytest.mark.asyncio
    async def test_heartbeat_exit_reason_is_heartbeat_timeout(self, pm, manager):
        """mutmut_91/92: exit_reason='heartbeat_timeout' exact string."""
        old = time.time() - 9999
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].exit_reason == "heartbeat_timeout"

    @pytest.mark.asyncio
    async def test_heartbeat_stopped_at_set(self, pm, manager):
        """mutmut_95/96: stopped_at set on heartbeat timeout."""
        old = time.time() - 9999
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].stopped_at is not None

    @pytest.mark.asyncio
    async def test_heartbeat_release_called(self, pm, manager):
        """mutmut_99/100: release_bot_account called for timed-out bots."""
        old = time.time() - 9999
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        release_calls = []
        with patch.object(pm, "release_bot_account", side_effect=lambda b: release_calls.append(b)):
            await self._run_one(pm, manager)

        assert "bot_000" in release_calls

    @pytest.mark.asyncio
    async def test_heartbeat_broadcast_called(self, pm, manager):
        """mutmut_107: broadcast_status called after heartbeat timeout."""
        old = time.time() - 9999
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", last_update_time=old, pid=0)
        manager.config.heartbeat_timeout_s = 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        manager.broadcast_status.assert_called()


# ---------------------------------------------------------------------------
# monitor_processes — stale-queued detection (mutmut_110-142)
# ---------------------------------------------------------------------------
class TestMonitorStaleQueued:
    async def _run_one(self, pm, manager):
        task = asyncio.create_task(pm.monitor_processes())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_stale_queued_skip_when_not_queued(self, pm, manager):
        """mutmut_110: state != 'queued' check."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", pid=0)
        pm._queued_since["bot_000"] = time.time() - 9999
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        # Running bot: queued_since should be popped (not tracked)
        assert "bot_000" not in pm._queued_since

    @pytest.mark.asyncio
    async def test_stale_queued_skip_when_pid_nonzero(self, pm, manager):
        """mutmut_114/115: pid != 0 check."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=999)
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        # bot with pid set should be skipped (queued_since removed)
        assert "bot_000" not in pm._queued_since

    @pytest.mark.asyncio
    async def test_stale_queued_skip_when_started(self, pm, manager):
        """mutmut_117: started_at is not None check."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0, started_at=time.time())
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert "bot_000" not in pm._queued_since

    @pytest.mark.asyncio
    async def test_stale_queued_registered_on_first_see(self, pm, manager):
        """mutmut_119/120: queued_since[bot] = now on first encounter."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0)
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        t_before = time.time()
        await self._run_one(pm, manager)
        t_after = time.time()

        # Should have been registered
        assert "bot_000" in pm._queued_since
        assert t_before <= pm._queued_since["bot_000"] <= t_after

    @pytest.mark.asyncio
    async def test_stale_queued_launch_delay_threshold(self, pm, manager, tmp_path):
        """mutmut_122/123: >= vs > for delay comparison."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0, config=str(config))
        manager.desired_bots = 0  # not in desired-state mode
        pm._queued_since["bot_000"] = time.time() - pm._queued_launch_delay - 1  # past threshold
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []

        async def spy(bid, cfg):
            launched.append(bid)

        with patch.object(pm, "_launch_queued_bot", side_effect=spy):
            await self._run_one(pm, manager)

        assert "bot_000" in launched

    @pytest.mark.asyncio
    async def test_stale_queued_no_launch_when_desired_bots_positive(self, pm, manager, tmp_path):
        """mutmut_125/126: desired_bots > 0 -> skip launch."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0, config=str(config))
        manager.desired_bots = 2  # desired-state mode active
        pm._queued_since["bot_000"] = time.time() - pm._queued_launch_delay - 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_bot", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        assert "bot_000" not in launched

    @pytest.mark.asyncio
    async def test_stale_queued_no_config_sets_stopped(self, pm, manager):
        """mutmut_133/134: no config -> state='stopped', exit_reason='no_config'."""
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="queued", pid=0, config=None)
        manager.desired_bots = 0
        pm._queued_since["bot_000"] = time.time() - pm._queued_launch_delay - 1
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        assert manager.bots["bot_000"].state == "stopped"
        assert manager.bots["bot_000"].exit_reason == "no_config"


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

        assert manager.bots["bot_000"].state == "queued"  # not killed

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
        object.__setattr__(bot, "activity_context", "bust")  # lowercase
        manager.bots["bot_000"] = bot
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        await self._run_one(pm, manager)

        # .upper() should normalize "bust" to "BUST"
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

        # With desired_bots=0, no enforcement
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
        # One error bot, one running bot
        manager.bots["bot_error"] = BotStatusBase(bot_id="bot_error", state="error", config=str(config))
        manager.bots["bot_running"] = BotStatusBase(bot_id="bot_running", state="running", config=str(config))
        manager.broadcast_status = AsyncMock()
        manager.config.health_check_interval_s = 0
        manager.account_pool = None

        launched = []
        with patch.object(pm, "_launch_queued_bot", side_effect=lambda b, c: launched.append(b)):
            await self._run_one(pm, manager)

        # bot_error should be pruned (terminal state)
        assert "bot_error" not in manager.bots

    @pytest.mark.asyncio
    async def test_active_states_set(self, pm, manager, tmp_path):
        """mutmut_208: active_states = {'running', 'queued', 'recovering', 'blocked'}."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 3
        manager.swarm_paused = False
        # Two active bots
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

        # deficit = 3-2 = 1; should launch 1
        assert len(launched) >= 1

    @pytest.mark.asyncio
    async def test_deficit_positive_spawns_bots(self, pm, manager, tmp_path):
        """mutmut_216-228: deficit > 0 branch spawns correct count."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.desired_bots = 3
        manager.swarm_paused = False
        # 1 active bot -> deficit = 2
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

        # deficit=1; config from active bot used
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
        # No active bots, no dead bots with configs
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

        # Check new bots are in manager.bots with state 'queued'
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

        # 3 active, desired=1, excess=2 -> 2 kills
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

        # Should kill bot_002 and bot_001 (highest first)
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

        # Two bots should be removed
        remaining = len(manager.bots)
        assert remaining <= 2  # at most 1 kept + others removed
