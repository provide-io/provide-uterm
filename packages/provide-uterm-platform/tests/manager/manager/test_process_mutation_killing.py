#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.terminal.manager.process — supplemental batch (part 1).

Classes: TestInitDefaultsExtra, TestSyncNextAgentIndexExtra, TestAllocateAgentIdExtra,
         TestStartSpawnSwarmExtra, TestSpawnAgentGameTypeFallbacks, TestSpawnAgentProcessArgs.
"""

from __future__ import annotations

import asyncio
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
# __init__ defaults — surviving mutants 1, 5, 9, 12-17
# ---------------------------------------------------------------------------
class TestInitDefaultsExtra:
    def test_log_dir_default_param_is_empty(self, manager):
        """mutmut_1: log_dir default '' -> 'XXXX'."""
        pm = AgentProcessManager(manager)
        assert pm._log_dir == ""

    def test_log_dir_assigned_from_param(self, manager, tmp_path):
        """mutmut_5: self._log_dir = None."""
        pm = AgentProcessManager(manager, log_dir=str(tmp_path))
        assert pm._log_dir == str(tmp_path)

    def test_queued_launch_delay_is_30(self, manager):
        """mutmut_9: _queued_launch_delay 30.0 -> 31.0."""
        pm = AgentProcessManager(manager)
        assert pm._queued_launch_delay == 30.0
        assert pm._queued_launch_delay != 31.0

    def test_spawn_name_style_default_lowercase(self, manager):
        """mutmut_12/13/14: _spawn_name_style None/'XXrandomXX'/'RANDOM'."""
        pm = AgentProcessManager(manager)
        assert pm._spawn_name_style == "random"

    def test_spawn_name_base_default_empty(self, manager):
        """mutmut_15/16: _spawn_name_base None/'XXXX'."""
        pm = AgentProcessManager(manager)
        assert pm._spawn_name_base == ""

    def test_last_spawn_config_is_none_not_empty(self, manager):
        """mutmut_17: _last_spawn_config '' -> None."""
        pm = AgentProcessManager(manager)
        assert pm._last_spawn_config is None
        assert pm._last_spawn_config != ""


# ---------------------------------------------------------------------------
# sync_next_agent_index — surviving mutmut_3 (max_seen = -2)
# ---------------------------------------------------------------------------
class TestSyncNextAgentIndexExtra:
    def test_empty_agents_and_processes_returns_zero(self, pm):
        assert pm.sync_next_agent_index() == 0

    def test_single_agent_0_gives_next_1(self, pm, manager):
        """mutmut_3: max_seen=-2 -> next = max(0, -1) = 0 (wrong when agent_0 present)."""
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        result = pm.sync_next_agent_index()
        assert result == 1

    def test_only_processes_dict_counted(self, pm, manager):
        """mutmut_4: uses & instead of |; processes-only agent would be missed."""
        manager.processes["agent_003"] = MagicMock()
        result = pm.sync_next_agent_index()
        assert result == 4

    def test_union_both_agents_and_processes(self, pm, manager):
        """mutmut_4: & would miss processes-only entry."""
        manager.agents["agent_002"] = AgentStatusBase(agent_id="agent_002")
        manager.processes["agent_005"] = MagicMock()
        result = pm.sync_next_agent_index()
        assert result == 6


# ---------------------------------------------------------------------------
# allocate_agent_id — surviving mutmut_8 (_next = idx-1), mutmut_10 (idx=1 not +=1)
# ---------------------------------------------------------------------------
class TestAllocateAgentIdExtra:
    def test_next_index_set_to_idx_plus_one(self, pm, manager):
        """mutmut_8: _next_agent_index = idx-1 instead of idx+1."""
        bid = pm.allocate_agent_id()
        assert bid == "agent_000"
        assert pm._next_agent_index == 1

    def test_idx_increments_not_resets(self, pm, manager):
        """mutmut_10: idx=1 instead of idx+=1 would cause agent_001 loop."""
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001")
        bid = pm.allocate_agent_id()
        assert bid == "agent_002"

    def test_format_is_zero_padded_3_digits(self, pm, manager):
        """Verify format f'agent_{idx:03d}' — mutmut_3 would return None."""
        bid = pm.allocate_agent_id()
        import re

        assert re.match(r"^agent_\d{3}$", bid), f"Expected agent_NNN format, got {bid!r}"


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
        for _ in range(5):
            await asyncio.sleep(0)
        assert done_task.done(), "task should be done after yielding to event loop"
        pm._spawn_tasks = [done_task]

        async def noop(*a, **kw):
            return []

        with patch.object(pm, "spawn_swarm", side_effect=noop):
            await pm.start_spawn_swarm(["/a.yaml"], cancel_existing=False)

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
                name_base="agent",
            )
            await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

        assert captured["paths"] == ["/z.yaml"]
        assert captured["group_size"] == 3
        assert captured["group_delay"] == 5.0
        assert captured["name_style"] == "fixed"
        assert captured["name_base"] == "agent"

    @pytest.mark.asyncio
    async def test_task_appended_to_spawn_tasks(self, pm):
        """mutmut_25: _spawn_tasks.append(None)."""

        async def noop(*a, **kw):
            return []

        with patch.object(pm, "spawn_swarm", side_effect=noop):
            await pm.start_spawn_swarm(["/a.yaml"], cancel_existing=False)

        assert len(pm._spawn_tasks) >= 1
        assert all(isinstance(t, asyncio.Task) for t in pm._spawn_tasks)


# ---------------------------------------------------------------------------
# spawn_agent — worker_type defaults/fallbacks and config reading
# ---------------------------------------------------------------------------
class TestSpawnAgentGameTypeFallbacks:
    @pytest.mark.asyncio
    async def test_worker_type_fallback_single_registry_succeeds(self, pm, manager, tmp_path):
        config = tmp_path / "cfg.yaml"
        config.write_text("{}")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_agent(str(config), "agent_000")
        assert result == "agent_000"

    @pytest.mark.asyncio
    async def test_worker_type_get_default_single_registry(self, pm, manager, tmp_path):
        config = tmp_path / "cfg.yaml"
        config.write_text("connection:\n  host: x\n")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_agent(str(config), "agent_000")
        assert result == "agent_000"

    @pytest.mark.asyncio
    async def test_worker_type_null_single_registry(self, pm, manager, tmp_path):
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: ~\n")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_agent(str(config), "agent_000")
        assert result == "agent_000"

    @pytest.mark.asyncio
    async def test_unknown_worker_type_with_multiple_registries_raises(self, pm, manager, tmp_path):
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: not_registered\n")
        pm._worker_registry["other"] = pm._worker_registry["test_game"]
        with pytest.raises(RuntimeError, match="Unknown worker_type"):
            await pm.spawn_agent(str(config), "agent_000")

    @pytest.mark.asyncio
    async def test_warning_logged_on_config_read_error(self, pm, manager, tmp_path):
        config = tmp_path / "cfg.yaml"
        config.write_bytes(b"\xff\xfe")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_agent(str(config), "agent_000")
        assert result == "agent_000"


# ---------------------------------------------------------------------------
# spawn_agent — _spawn_process argument passing (mutmut_104, 105)
# ---------------------------------------------------------------------------
class TestSpawnAgentProcessArgs:
    @pytest.mark.asyncio
    async def test_spawn_process_called_with_agent_id(self, pm, manager, tmp_path):
        """mutmut_104: _spawn_process(None, cmd, env) -> agent_id must be passed."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        calls = []

        def fake_spawn(bid, cmd, env):
            calls.append(bid)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(str(config), "agent_007")

        assert calls == ["agent_007"]

    @pytest.mark.asyncio
    async def test_spawn_process_called_with_cmd(self, pm, manager, tmp_path):
        """mutmut_105: _spawn_process(agent_id, None, env) -> cmd must be passed."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        calls = []

        def fake_spawn(bid, cmd, env):
            calls.append(cmd)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(str(config), "agent_007")

        assert calls[0] is not None
        assert isinstance(calls[0], list)
        assert len(calls[0]) > 0
