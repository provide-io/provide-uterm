#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — GetRegistryEntry, KillAgentResidual, AllocateAgentIdNoInfiniteLoop, SpawnSwarmTimeout, Mon."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.process import AgentProcessManager


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


class TestGetRegistryEntryKills:
    def test_unknown_type_with_single_entry_raises_not_returns(self, pm):
        """mutmut_4: `and` -> `or`.

        With exactly 1 registered entry and an unknown (non-'default')
        worker_type, the original requires BOTH `len==1 AND type=='default'`,
        so it raises RuntimeError. The mutant (`or`) sees `len==1` is True and
        wrongly returns the sole entry instead of raising.
        """
        assert len(pm._worker_registry) == 1
        with pytest.raises(RuntimeError) as exc:
            pm._get_registry_entry("nope_not_default", "/cfg/path.toml")
        msg = str(exc.value)
        assert "nope_not_default" in msg
        assert "/cfg/path.toml" in msg

    def test_default_fallback_still_returns_sole_entry(self, pm):
        """Lock the legitimate 'default' fallback (passes on original + mutant)."""
        entry = pm._get_registry_entry("default", "/cfg/path.toml")
        assert entry is pm._worker_registry["test_game"]

    def test_known_type_returns_entry(self, pm):
        """Direct hit returns the matching entry."""
        entry = pm._get_registry_entry("test_game", "/cfg/path.toml")
        assert entry is pm._worker_registry["test_game"]


# ---- kill_agent residual misses (mutmut_17/20/23) ----


class TestKillAgentResidualKills:
    """Kills the kill_agent fallback_pid mutants the first pass missed.

    The original computes ``fallback_pid = int(getattr(agent, "pid", 0) or 0)
    if process is None else 0`` and forwards ``pid=fallback_pid or None`` to
    ``_stop_process_tree`` — so pid is ALWAYS None on these paths.
    """

    async def test_unknown_agent_forwards_pid_none(self, pm):
        # mutmut_17: getattr(agent, "pid") drops the default -> AttributeError on
        # the None agent. mutmut_20: default 1 -> fallback_pid 1 -> pid=1.
        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as stop:
            await pm.kill_agent("ghost-unknown")
        stop.assert_awaited_once()
        assert stop.await_args.kwargs["pid"] is None

    async def test_live_process_forwards_pid_none(self, pm, manager):
        # mutmut_23: ``else 0`` -> ``else 1`` makes fallback_pid 1 (and pid=1)
        # when a live process exists; original keeps pid=None (process carries it).
        manager.processes["agent_007"] = make_mock_proc(pid=999)
        manager.agents["agent_007"] = manager._agent_status_class(
            agent_id="agent_007", pid=999, config="c", state="running"
        )
        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as stop:
            await pm.kill_agent("agent_007")
        stop.assert_awaited_once()
        assert stop.await_args.kwargs["pid"] is None


# ---- timeout-class kills: infinite-loop / runaway-sleep mutants ----


class TestAllocateAgentIdNoInfiniteLoop:
    async def test_allocate_returns_first_free_without_spinning(self, pm):
        """Kills allocate_agent_id mutmut_5/6: inverting a ``not in`` membership
        check makes the ``while True`` loop never return (it would hang forever).
        A membership container that raises after a few probes turns the hang into a
        fast RuntimeError for the mutants, while the original returns on probe 1.
        """

        class _GuardedAgents(dict):
            def __init__(self) -> None:
                super().__init__()
                self._probes = 0

            def __contains__(self, key: object) -> bool:
                self._probes += 1
                if self._probes > 5:
                    raise RuntimeError("allocate_agent_id spun without terminating")
                return dict.__contains__(self, key)

        pm.manager.agents = _GuardedAgents()
        pm.manager.processes = {}
        assert pm.allocate_agent_id() == "agent_000"


class TestSpawnSwarmTimeoutKills:
    async def test_no_sleep_after_final_group(self, pm):
        # mutmut_63: ``group_end < total`` -> ``<= total`` adds an EXTRA sleep after
        # the last group (group_end == total) that the original skips.
        with (
            patch.object(pm, "spawn_agent", new_callable=AsyncMock),
            patch("provide.uterm.manager.process_impl.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch.object(pm.manager, "broadcast_status", new_callable=AsyncMock),
        ):
            await pm.spawn_swarm(["a.yaml", "b.yaml"], group_size=5, group_delay=0.0)
        sleep.assert_not_awaited()

    async def test_all_configs_spawned_across_groups(self, pm):
        # mutmut_41: ``group_start + group_size`` -> ``- group_size`` makes group_end
        # land before group_start, so the per-group slices spawn fewer/no agents.
        cfgs = ["a.yaml", "b.yaml", "c.yaml"]
        with (
            patch.object(pm, "spawn_agent", new_callable=AsyncMock) as spawn,
            patch("provide.uterm.manager.process_impl.asyncio.sleep", new_callable=AsyncMock),
            patch.object(pm.manager, "broadcast_status", new_callable=AsyncMock),
        ):
            await pm.spawn_swarm(cfgs, group_size=2, group_delay=0.0)
        assert spawn.await_count == len(cfgs)


# ---- monitor_processes sleep-arg kill (mutmut_20) ----


class TestMonitorSleepArgKill:
    _PI = "provide.uterm.manager.process_impl"

    async def test_loop_does_not_crash_on_normal_iteration_sleep(self, pm):
        """Kills monitor_processes mutmut_20: ``asyncio.sleep(health_check_interval)``
        -> ``asyncio.sleep(None)``. Real (and the conftest's min-clamped) asyncio.sleep
        raises TypeError on None, so the mutant crashes the monitor task on iteration 1;
        the original keeps looping. We assert the task is still running (not crashed)
        after a few scheduler turns.
        """
        with patch.multiple(
            self._PI,
            _handle_exited_processes=AsyncMock(),
            _handle_heartbeat_timeouts=AsyncMock(),
            _handle_stale_queued=MagicMock(),
            _handle_bust_respawn=AsyncMock(),
            _handle_desired_state=AsyncMock(),
        ):
            pm.manager.health_check_interval = 0
            task = asyncio.create_task(pm.monitor_processes())
            for _ in range(5):
                await asyncio.sleep(0)
            crashed = task.done()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not crashed
