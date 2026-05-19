#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.manager.process."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import provide.uterm.manager.process as process_module
from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.models import AgentStatusBase
from provide.uterm.manager.process import AgentProcessManager


class FakeWorkerPlugin:
    """Minimal WorkerRegistryPlugin implementation for tests."""

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
    pm = AgentProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.agent_process_manager = pm
    return pm


class TestAgentIdManagement:
    def test_parse_agent_index(self):
        assert AgentProcessManager._parse_agent_index("agent_000") == 0
        assert AgentProcessManager._parse_agent_index("agent_042") == 42
        assert AgentProcessManager._parse_agent_index("invalid") is None
        assert AgentProcessManager._parse_agent_index("") is None
        assert AgentProcessManager._parse_agent_index("agent_") is None

    def test_allocate_agent_id(self, pm, manager):
        bid = pm.allocate_agent_id()
        assert bid == "agent_000"
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        bid2 = pm.allocate_agent_id()
        assert bid2 == "agent_001"

    def test_sync_next_agent_index(self, pm, manager):
        manager.agents["agent_005"] = AgentStatusBase(agent_id="agent_005")
        manager.agents["agent_010"] = AgentStatusBase(agent_id="agent_010")
        idx = pm.sync_next_agent_index()
        assert idx == 11

    def test_note_agent_id(self, pm):
        pm.note_agent_id("agent_050")
        assert pm._next_agent_index == 51
        pm.note_agent_id("invalid_id")
        assert pm._next_agent_index == 51  # unchanged

    def test_allocate_skips_existing(self, pm, manager):
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        manager.agents["agent_001"] = AgentStatusBase(agent_id="agent_001")
        bid = pm.allocate_agent_id()
        assert bid == "agent_002"


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
        pm.release_agent_account("agent_000")  # should not raise

    def test_with_pool(self, pm, manager):
        pool = MagicMock()
        pool.release_by_agent.return_value = True
        manager.account_pool = pool
        pm.release_agent_account("agent_000")
        pool.release_by_agent.assert_called_once_with(agent_id="agent_000", cooldown_s=0)

    def test_pool_error(self, pm, manager):
        pool = MagicMock()
        pool.release_by_agent.side_effect = RuntimeError("fail")
        manager.account_pool = pool
        pm.release_agent_account("agent_000")  # should not raise


class TestSpawnAgent:
    @pytest.mark.asyncio
    async def test_max_agents_reached(self, pm, manager):
        manager.max_agents = 0
        with pytest.raises(RuntimeError, match="Max agents"):
            await pm.spawn_agent("/config.yaml", "agent_000")

    @pytest.mark.asyncio
    async def test_config_not_found(self, pm):
        with pytest.raises(RuntimeError, match="Config not found"):
            await pm.spawn_agent("/nonexistent.yaml", "agent_000")

    @pytest.mark.asyncio
    async def test_unknown_worker_type(self, pm, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: unknown_game\n")
        with pytest.raises(RuntimeError, match="Unknown worker_type"):
            await pm.spawn_agent(str(config), "agent_000")

    @pytest.mark.asyncio
    async def test_single_registry_fallback(self, pm, manager, tmp_path):
        """No worker_type in YAML + single registry entry → uses that entry."""
        config = tmp_path / "test.yaml"
        config.write_text("# no worker_type key\n")

        mock_proc = MagicMock()
        mock_proc.pid = 7777
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            result = await pm.spawn_agent(str(config), "agent_000")

        assert result == "agent_000"
        assert manager.agents["agent_000"].state == "running"

    @pytest.mark.asyncio
    async def test_spawn_success(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            result = await pm.spawn_agent(str(config), "agent_000")

        assert result == "agent_000"
        assert "agent_000" in manager.agents
        assert manager.agents["agent_000"].pid == 12345
        assert manager.agents["agent_000"].state == "running"

    @pytest.mark.asyncio
    async def test_spawn_updates_existing_agent(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            await pm.spawn_agent(str(config), "agent_000")

        assert manager.agents["agent_000"].pid == 9999
        assert manager.agents["agent_000"].state == "running"

    @pytest.mark.asyncio
    async def test_spawn_process_failure(self, pm, manager, tmp_path):
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with (
            patch.object(pm, "_spawn_process", side_effect=OSError("no such file")),
            pytest.raises(RuntimeError, match="Failed to spawn"),
        ):
            await pm.spawn_agent(str(config), "agent_000")

    @pytest.mark.asyncio
    async def test_spawn_bad_yaml_with_multiple_registries(self, pm, manager, tmp_path):
        """Bad YAML → falls to 'default'; with multiple registries, raises Unknown worker_type."""
        config = tmp_path / "test.yaml"
        config.write_text("{{invalid yaml")
        # Add a second registry entry so fallback doesn't apply
        from unittest.mock import MagicMock

        pm._worker_registry["other_game"] = MagicMock()
        with pytest.raises(RuntimeError, match="Unknown worker_type"):
            await pm.spawn_agent(str(config), "agent_000")


class TestKillAgent:
    @pytest.mark.asyncio
    async def test_kill_unknown(self, pm, manager):
        manager.broadcast_status = AsyncMock()
        await pm.kill_agent("nonexistent")
        # Should not raise

    @pytest.mark.asyncio
    async def test_kill_terminates(self, pm, manager):
        mock_proc = MagicMock()
        manager.processes["agent_000"] = mock_proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as mock_stop:
            await pm.kill_agent("agent_000")
        mock_stop.assert_awaited_once_with(
            agent_id="agent_000", process=mock_proc, pid=None, timeout_s=process_module._STOP_TIMEOUT_S
        )
        assert manager.agents["agent_000"].state == "stopped"
        assert "agent_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_kill_uses_pid_fallback_when_process_not_tracked(self, pm, manager):
        agent = AgentStatusBase(agent_id="agent_000", state="running")
        agent.pid = 9876  # type: ignore[attr-defined]
        manager.agents["agent_000"] = agent
        # process not in manager.processes — simulate post-restart scenario
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as mock_stop:
            await pm.kill_agent("agent_000")
        mock_stop.assert_awaited_once_with(
            agent_id="agent_000", process=None, pid=9876, timeout_s=process_module._STOP_TIMEOUT_S
        )
        assert manager.agents["agent_000"].state == "stopped"

    @pytest.mark.asyncio
    async def test_kill_force_on_timeout(self, pm, manager):
        mock_proc = MagicMock()
        manager.processes["agent_000"] = mock_proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "_stop_process_tree", new_callable=AsyncMock) as mock_stop:
            await pm.kill_agent("agent_000")
        mock_stop.assert_awaited_once_with(
            agent_id="agent_000", process=mock_proc, pid=None, timeout_s=process_module._STOP_TIMEOUT_S
        )


class TestProcessTreeHelpers:
    def test_spawn_process_posix_sets_start_new_session(self, pm, tmp_path):
        pm._log_dir = str(tmp_path / "logs")
        with (
            patch.object(process_module.os, "name", "posix"),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value = MagicMock(pid=123)
            pm._spawn_process("agent_000", ["python", "-c", "pass"], {})
        assert mock_popen.call_args.kwargs["start_new_session"] is True

    def test_spawn_process_windows_sets_creationflags(self, pm, tmp_path):
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(process_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
        ):
            assert pm._spawn_platform_kwargs() == {"creationflags": 512}

    @pytest.mark.asyncio
    async def test_stop_process_tree_posix_graceful(self, pm):
        proc = MagicMock(pid=321)
        with (
            patch.object(process_module.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock),
            patch.object(pm, "_signal_posix_process_group") as mock_signal,
        ):
            await pm._stop_process_tree(agent_id="agent_000", process=proc)
        mock_signal.assert_called_once_with(321, process_module.signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_stop_process_tree_posix_timeout_escalates(self, pm):
        proc = MagicMock(pid=654)
        with (
            patch.object(process_module.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock) as mock_wait,
            patch.object(pm, "_signal_posix_process_group") as mock_signal,
        ):
            mock_wait.side_effect = [TimeoutError, None]
            await pm._stop_process_tree(agent_id="agent_000", process=proc)
        assert mock_signal.call_args_list == [
            call(654, process_module.signal.SIGTERM),
            call(654, process_module.signal.SIGKILL),
        ]

    @pytest.mark.asyncio
    async def test_stop_process_tree_windows_uses_taskkill_immediately(self, pm):
        # On Windows, terminate() only kills the direct child; taskkill /T /F
        # must be used immediately so the whole process tree is killed.
        proc = MagicMock(pid=987)
        with (
            patch.object(process_module.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock) as mock_wait,
            patch.object(pm, "_taskkill_process_tree", new_callable=AsyncMock) as mock_taskkill,
        ):
            mock_wait.return_value = None
            await pm._stop_process_tree(agent_id="agent_000", process=proc)
        proc.terminate.assert_not_called()
        mock_taskkill.assert_awaited_once_with(987)

    @pytest.mark.asyncio
    async def test_stop_process_tree_ignores_non_int_mock_pid(self, pm):
        proc = MagicMock()
        with patch.object(pm, "_signal_posix_process_group") as mock_signal:
            await pm._stop_process_tree(agent_id="agent_000", process=proc)
        mock_signal.assert_not_called()

    def test_try_set_subreaper_linux(self, pm):
        """_try_set_subreaper calls prctl(PR_SET_CHILD_SUBREAPER) on Linux."""
        mock_libc = MagicMock()
        mock_libc.prctl.return_value = 0
        with (
            patch.object(process_module.sys, "platform", "linux"),
            patch("ctypes.CDLL", return_value=mock_libc),
        ):
            pm._try_set_subreaper()
        mock_libc.prctl.assert_called_once_with(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER

    def test_try_set_subreaper_non_linux_skips(self, pm):
        """_try_set_subreaper is a no-op on non-Linux platforms."""
        with patch.object(process_module.sys, "platform", "darwin"):
            pm._try_set_subreaper()  # must not raise

    def test_try_set_subreaper_ctypes_error_ignored(self, pm):
        """_try_set_subreaper swallows exceptions from ctypes."""
        with (
            patch.object(process_module.sys, "platform", "linux"),
            patch("ctypes.CDLL", side_effect=OSError("no libc")),
        ):
            pm._try_set_subreaper()  # must not raise

    @pytest.mark.asyncio
    async def test_kill_agent_terminates_spawned_child_process_tree(self, pm, manager, tmp_path):
        parent_script = tmp_path / "parent.py"
        child_script = tmp_path / "child.py"
        child_pid_file = tmp_path / "child.pid"
        heartbeat_file = tmp_path / "child.heartbeat"

        child_script.write_text(
            "import pathlib\n"
            "import sys\n"
            "import time\n"
            "\n"
            "heartbeat = pathlib.Path(sys.argv[1])\n"
            "while True:\n"
            "    heartbeat.write_text(str(time.time()))\n"
            "    time.sleep(0.05)\n"
        )
        parent_script.write_text(
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "\n"
            "child_script = sys.argv[1]\n"
            "heartbeat = sys.argv[2]\n"
            "pid_file = pathlib.Path(sys.argv[3])\n"
            "child = subprocess.Popen([sys.executable, child_script, heartbeat])\n"
            "pid_file.write_text(str(child.pid))\n"
            "time.sleep(60)\n"
        )

        process = pm._spawn_process(
            "agent_123",
            [sys.executable, str(parent_script), str(child_script), str(heartbeat_file), str(child_pid_file)],
            dict(os.environ),
        )
        manager.processes["agent_123"] = process
        manager.agents["agent_123"] = AgentStatusBase(agent_id="agent_123", state="running", pid=process.pid)
        manager.broadcast_status = AsyncMock()

        try:
            deadline = time.time() + 5.0
            while time.time() < deadline and not child_pid_file.exists():
                await asyncio.sleep(0.05)
            assert child_pid_file.exists(), "child process pid file was never created"

            deadline = time.time() + 5.0
            while time.time() < deadline and not heartbeat_file.exists():
                await asyncio.sleep(0.05)
            assert heartbeat_file.exists(), "child heartbeat file was never created"

            first_mtime = heartbeat_file.stat().st_mtime_ns
            await asyncio.sleep(0.2)
            second_mtime = heartbeat_file.stat().st_mtime_ns
            assert second_mtime > first_mtime, "child heartbeat did not advance before shutdown"

            await pm.kill_agent("agent_123")

            # Wait for the parent process to actually exit. ``kill_agent``
            # only awaits its own bookkeeping — the kernel still needs to
            # deliver and reap the signal across parent + child.
            deadline = time.time() + 5.0
            while time.time() < deadline and process.poll() is None:
                await asyncio.sleep(0.05)
            assert process.poll() is not None, "parent process did not exit within 5s of kill_agent()"

            # Wait for the heartbeat to stabilise — sample until two
            # consecutive readings show the same mtime, then assert that
            # remains true through the platform-specific settle window.
            # Polling avoids the fixed-sleep flake on slow runners.
            settle_window = 0.5 if os.name == "nt" else 0.25
            stable_deadline = time.time() + 5.0
            stopped_mtime = heartbeat_file.stat().st_mtime_ns
            while time.time() < stable_deadline:
                await asyncio.sleep(0.05)
                current = heartbeat_file.stat().st_mtime_ns
                if current == stopped_mtime:
                    break
                stopped_mtime = current
            await asyncio.sleep(settle_window)
            assert heartbeat_file.stat().st_mtime_ns == stopped_mtime, (
                "child heartbeat kept advancing after kill_agent()"
            )
        finally:
            if process.poll() is None:
                await pm._stop_process_tree(agent_id="agent_123", process=process)
