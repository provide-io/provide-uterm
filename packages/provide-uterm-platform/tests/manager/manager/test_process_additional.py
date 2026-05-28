#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Additional coverage tests for provide.uterm.manager.process."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import provide.uterm.manager.process as process_module
import provide.uterm.manager.process_impl as process_impl_module
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.models import AgentStatusBase
from provide.uterm.manager.process import AgentProcessManager


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_worker_module"

    def configure_worker_env(self, env, agent_status, manager, **kwargs):
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
    return AgentManager(config)


@pytest.fixture
def pm(manager, tmp_path):
    proc_mgr = AgentProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.agent_process_manager = proc_mgr
    return proc_mgr


class TestAdditionalCoverage:
    def test_allocate_agent_id_forces_collision_increment(self, pm, manager):
        pm._next_agent_index = 0
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        assert pm.allocate_agent_id() == "agent_001"

    @pytest.mark.asyncio
    async def test_start_spawn_swarm_cancels_existing_and_tracks_task(self, pm):
        sleeper = asyncio.create_task(asyncio.sleep(10))
        pm._spawn_tasks = [sleeper]
        with patch.object(pm, "spawn_swarm", new_callable=AsyncMock):
            await pm.start_spawn_swarm(["a.yaml"], cancel_existing=True)
        assert sleeper.cancelled()
        assert len(pm._spawn_tasks) == 1

    @pytest.mark.asyncio
    async def test_spawn_agent_rejected_by_policy(self, pm, tmp_path):
        cfg = tmp_path / "test.yaml"
        cfg.write_text("worker_type: test_game\n")
        pm.set_policy_gate(MagicMock(intercept_spawn=AsyncMock(return_value=False)))
        with pytest.raises(RuntimeError, match="rejected by policy"):
            await pm.spawn_agent(str(cfg), "agent_000")

    def test_build_worker_env_sets_name_base(self, pm):
        pm._spawn_name_style = "random"
        pm._spawn_name_base = "alpha"
        env = pm._build_worker_env("UTERM_WORKER_", None, FakeWorkerPlugin(), {})
        assert env["UTERM_WORKER_NAME_BASE"] == "alpha"

    def test_spawn_process_rotates_old_log_and_handles_popen_error(self, pm, tmp_path):
        pm._log_dir = str(tmp_path / "logs")
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "agent_001.log"
        log_file.write_bytes(b"x" * 4096)
        with (
            patch("provide.uterm.manager.constants.WORKER_LOG_MAX_BYTES", 1),
            patch("subprocess.Popen", side_effect=OSError("boom")),
            pytest.raises(OSError),
        ):
            pm._spawn_process("agent_001", ["python", "-c", "pass"], {})
        assert (log_dir / "agent_001.log.prev").exists()

    def test_build_preexec_rlimit_fn_windows_and_missing_resource(self, pm):
        with patch.object(process_module.os, "name", "nt"):
            assert pm._build_preexec_rlimit_fn() is None
        with (
            patch.object(process_module.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": None}),
        ):
            assert pm._build_preexec_rlimit_fn() is None

    def test_build_preexec_rlimit_nofile_defaults_soft_from_hard(self, pm):
        pm.manager.config.worker_rlimit_nofile_hard = 222
        pm.manager.config.worker_rlimit_nofile_soft = 0
        calls: list[tuple[int, tuple[int, int]]] = []

        class _FakeResource:
            RLIMIT_NOFILE = 7

            @staticmethod
            def setrlimit(kind: int, limits: tuple[int, int]) -> None:
                calls.append((kind, limits))

        with (
            patch.object(process_module.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": _FakeResource}),
        ):
            fn = pm._build_preexec_rlimit_fn()
            assert callable(fn)
            fn()
        assert calls == [(7, (222, 222))]

    @pytest.mark.asyncio
    async def test_wait_for_process_exit_coro_and_awaitable_paths(self, pm):
        proc1 = MagicMock()

        async def _w1() -> int:
            return 0

        proc1.wait = _w1
        await pm._wait_for_process_exit(proc1, 1.0)

        proc2 = MagicMock()

        class _Awaitable:
            def __await__(self):
                async def _inner():
                    return 0

                return _inner().__await__()

        proc2.wait.return_value = _Awaitable()
        await pm._wait_for_process_exit(proc2, 1.0)

    @pytest.mark.asyncio
    async def test_taskkill_process_tree_helper(self, pm):
        fake_proc = MagicMock()
        fake_proc.wait = AsyncMock(return_value=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)) as mock_exec:
            await pm._taskkill_process_tree(123)
        assert mock_exec.await_count == 1
        fake_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_process_tree_no_process_paths(self, pm):
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(pm, "_taskkill_process_tree", new_callable=AsyncMock) as mock_taskkill,
        ):
            await pm._stop_process_tree(agent_id="a", process=None, pid=77)
        mock_taskkill.assert_awaited_once_with(77)

        with (
            patch.object(process_module.os, "name", "posix"),
            patch.object(pm, "_signal_posix_process_group") as mock_signal,
        ):
            await pm._stop_process_tree(agent_id="b", process=None, pid=88)
        mock_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_swarm_grouping_and_failure_path(self, pm, manager):
        manager.broadcast_status = AsyncMock()
        with (
            patch.object(pm, "spawn_agent", new_callable=AsyncMock) as mock_spawn,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_spawn.side_effect = [None, RuntimeError("x"), None]
            out = await pm.spawn_swarm(["a", "b", "c"], group_size=2, group_delay=0.01)
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_launch_queued_agent_failure_marks_error(self, pm, manager):
        manager.agents["agent_009"] = AgentStatusBase(agent_id="agent_009", state="queued")
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "spawn_agent", new_callable=AsyncMock, side_effect=RuntimeError("bad")):
            await pm._launch_queued_agent("agent_009", "cfg")
        assert manager.agents["agent_009"].state == "error"
        assert manager.agents["agent_009"].exit_reason == "launch_failed"

    @pytest.mark.asyncio
    async def test_monitor_processes_single_iteration(self, pm, manager):
        manager.health_check_interval = 0.01
        with (
            patch.object(process_impl_module, "_handle_exited_processes", AsyncMock()),
            patch.object(process_impl_module, "_handle_heartbeat_timeouts", AsyncMock()),
            patch.object(process_impl_module, "_handle_stale_queued", MagicMock()),
            patch.object(process_impl_module, "_handle_bust_respawn", AsyncMock()),
            patch.object(process_impl_module, "_handle_desired_state", AsyncMock()),
            patch("asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            with pytest.raises(asyncio.CancelledError):
                await pm.monitor_processes()

    @pytest.mark.asyncio
    async def test_monitor_processes_triggers_hourly_log_cleanup(self, pm, manager):
        manager.health_check_interval = 0.01
        sleep_calls = {"count": 0}

        async def _sleep_side_effect(_delay: float) -> None:
            sleep_calls["count"] += 1
            if sleep_calls["count"] >= 360:
                raise asyncio.CancelledError

        with (
            patch.object(process_impl_module, "_handle_exited_processes", AsyncMock()),
            patch.object(process_impl_module, "_handle_heartbeat_timeouts", AsyncMock()),
            patch.object(process_impl_module, "_handle_stale_queued", MagicMock()),
            patch.object(process_impl_module, "_handle_bust_respawn", AsyncMock()),
            patch.object(process_impl_module, "_handle_desired_state", AsyncMock()),
            patch.object(process_impl_module, "_cleanup_old_worker_logs", MagicMock()) as mock_cleanup,
            patch("asyncio.sleep", AsyncMock(side_effect=_sleep_side_effect)),
        ):
            with pytest.raises(asyncio.CancelledError):
                await pm.monitor_processes()
        assert mock_cleanup.called
