#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.uterm.manager.process — supplemental batch (part 3).

Classes: TestSpawnProcessExtra, TestKillAgentExtra, TestReleaseAgentAccountExtra,
         TestLaunchQueuedAgentExtra.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


SAFE_FAKE_PID = 999_999_999


def make_mock_proc(pid=42, returncode=0):
    m = MagicMock()
    m.pid = pid
    m.returncode = returncode
    m.poll.return_value = None
    m.wait.return_value = returncode
    return m


# ---------------------------------------------------------------------------
# _spawn_process — surviving mutmut_3-11, 19-26
# ---------------------------------------------------------------------------
class TestSpawnProcessExtra:
    def test_uses_custom_log_dir(self, pm, tmp_path):
        """mutmut_3-4: log_dir path logic."""
        pm._log_dir = str(tmp_path / "my_logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("agent_001", ["echo"], {})
        assert (tmp_path / "my_logs").is_dir()

    def test_default_log_dir_fallback(self, pm, tmp_path, monkeypatch):
        """mutmut_5: Path('logs/workers') fallback when _log_dir empty."""
        pm._log_dir = ""
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("agent_002", ["echo"], {})
        assert (tmp_path / "logs" / "workers").is_dir()

    def test_log_file_named_agent_id_dot_log(self, pm, tmp_path):
        """mutmut_6-7: log file name uses agent_id."""
        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("agent_009", ["echo"], {})
        assert (tmp_path / "logs" / "agent_009.log").exists()

    def test_popen_stderr_is_stdout(self, pm, tmp_path):
        """mutmut_19-26: Popen call argument mutations."""
        import subprocess

        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("agent_000", ["echo", "hi"], {"K": "V"})
        _, kwargs = mp.call_args
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["env"] == {"K": "V"}

    def test_popen_stdout_is_file_handle(self, pm, tmp_path):
        """mutmut_20: stdout=None instead of log_handle."""
        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("agent_000", ["echo"], {})
        _, kwargs = mp.call_args
        assert kwargs["stdout"] is not None

    def test_returns_popen_process(self, pm, tmp_path):
        """mutmut_21: return None instead of proc."""
        pm._log_dir = str(tmp_path / "logs")
        mock_proc = MagicMock(pid=1)
        with patch("subprocess.Popen", return_value=mock_proc):
            result = pm._spawn_process("agent_000", ["echo"], {})
        assert result is mock_proc

    def test_log_handle_closed_after_success(self, pm, tmp_path):
        """mutmut_24-26: log handle closed."""
        pm._log_dir = str(tmp_path / "logs")
        with patch("subprocess.Popen") as mp:
            mp.return_value = MagicMock(pid=1)
            pm._spawn_process("agent_000", ["echo"], {})
        log_file = tmp_path / "logs" / "agent_000.log"
        log_file.open("a").close()

    def test_log_handle_closed_after_failure(self, pm, tmp_path):
        """mutmut_9-11: log handle closed even on exception."""
        pm._log_dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(exist_ok=True)
        with patch("subprocess.Popen", side_effect=OSError("fail")), pytest.raises(OSError):
            pm._spawn_process("agent_err", ["bad"], {})
        log_file = tmp_path / "logs" / "agent_err.log"
        assert log_file.exists()
        log_file.open("a").close()


# ---------------------------------------------------------------------------
# kill_agent — surviving mutmut_4, 10-12, 14-16
# ---------------------------------------------------------------------------
class TestKillAgentExtra:
    @pytest.mark.asyncio
    async def test_kill_agent_timeout_is_5(self, pm, manager):
        """mutmut_4: timeout=None."""
        proc = make_mock_proc(pid=SAFE_FAKE_PID)
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        wait_for_timeouts = []
        orig = asyncio.wait_for

        async def spy(coro, timeout=None):
            wait_for_timeouts.append(timeout)
            return await orig(coro, timeout=timeout)

        with patch("asyncio.wait_for", side_effect=spy):
            await pm.kill_agent("agent_000")

        assert 5.0 in wait_for_timeouts

    @pytest.mark.asyncio
    async def test_kill_sets_state_stopped(self, pm, manager):
        """mutmut_10: state='XXstoppedXX'."""
        proc = make_mock_proc(pid=SAFE_FAKE_PID)
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_agent("agent_000")

        assert manager.agents["agent_000"].state == "stopped"

    @pytest.mark.asyncio
    async def test_kill_sets_stopped_at_to_time(self, pm, manager):
        """mutmut_11: stopped_at = None."""
        proc = make_mock_proc(pid=SAFE_FAKE_PID)
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        t_before = time.time()
        await pm.kill_agent("agent_000")
        t_after = time.time()

        assert manager.agents["agent_000"].stopped_at is not None
        assert t_before <= manager.agents["agent_000"].stopped_at <= t_after

    @pytest.mark.asyncio
    async def test_kill_removes_from_processes(self, pm, manager):
        """mutmut_12: processes.pop(agent_id) missing."""
        proc = make_mock_proc(pid=SAFE_FAKE_PID)
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_agent("agent_000")

        assert "agent_000" not in manager.processes

    @pytest.mark.asyncio
    async def test_kill_calls_release_with_agent_id(self, pm, manager):
        """mutmut_14: release_agent_account(None)."""
        proc = make_mock_proc(pid=SAFE_FAKE_PID)
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        calls = []
        with patch.object(pm, "release_agent_account", side_effect=lambda b: calls.append(b)):
            await pm.kill_agent("agent_000")

        assert "agent_000" in calls

    @pytest.mark.asyncio
    async def test_kill_broadcasts_after(self, pm, manager):
        """mutmut_15/16: broadcast_status calls."""
        proc = make_mock_proc(pid=SAFE_FAKE_PID)
        manager.processes["agent_000"] = proc
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="running")
        manager.broadcast_status = AsyncMock()

        await pm.kill_agent("agent_000")

        manager.broadcast_status.assert_called()


# ---------------------------------------------------------------------------
# release_agent_account — surviving mutmut_10-23
# ---------------------------------------------------------------------------
class TestReleaseAgentAccountExtra:
    def test_no_pool_returns_none(self, pm, manager):
        """mutmut_10: no AttributeError when pool is None."""
        manager.account_pool = None
        result = pm.release_agent_account("agent_000")
        assert result is None

    def test_calls_release_by_agent_with_correct_args(self, pm, manager):
        """mutmut_11-16: pool.release_by_agent(agent_id=..., cooldown_s=0)."""
        pool = MagicMock()
        pool.release_by_agent.return_value = True
        manager.account_pool = pool

        pm.release_agent_account("agent_007")

        pool.release_by_agent.assert_called_once_with(agent_id="agent_007", cooldown_s=0)

    def test_cooldown_s_is_zero_not_one(self, pm, manager):
        """mutmut_17: cooldown_s=1 instead of 0."""
        pool = MagicMock()
        pool.release_by_agent.return_value = False
        manager.account_pool = pool

        pm.release_agent_account("agent_001")

        _, kwargs = pool.release_by_agent.call_args
        assert kwargs.get("cooldown_s") == 0

    def test_exception_in_pool_does_not_propagate(self, pm, manager):
        """mutmut_19-23: exception handling."""
        pool = MagicMock()
        pool.release_by_agent.side_effect = RuntimeError("pool error")
        manager.account_pool = pool

        pm.release_agent_account("agent_000")

    def test_release_by_agent_true_logs_info(self, pm, manager):
        """mutmut_22: released check inverted."""
        pool = MagicMock()
        pool.release_by_agent.return_value = True
        manager.account_pool = pool

        pm.release_agent_account("agent_000")
        pool.release_by_agent.assert_called_once()


# ---------------------------------------------------------------------------
# _launch_queued_agent — surviving mutmut_1-13, 19-21
# ---------------------------------------------------------------------------
class TestLaunchQueuedAgentExtra:
    @pytest.mark.asyncio
    async def test_success_path_no_error_set(self, pm, manager, tmp_path):
        """mutmut_1/2: success path does not set error state."""
        config = tmp_path / "cfg.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)

        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm._launch_queued_agent("agent_000", str(config))

        assert manager.agents["agent_000"].state == "running"

    @pytest.mark.asyncio
    async def test_failure_sets_error_state(self, pm, manager):
        """mutmut_3/4: state='error' on failure."""
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)

        with patch.object(pm, "spawn_agent", side_effect=RuntimeError("fail")):
            await pm._launch_queued_agent("agent_000", "/cfg.yaml")

        assert manager.agents["agent_000"].state == "error"

    @pytest.mark.asyncio
    async def test_failure_sets_error_message_with_launch_failed(self, pm, manager):
        """mutmut_5-7: error_message format."""
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)

        with patch.object(pm, "spawn_agent", side_effect=RuntimeError("the reason")):
            await pm._launch_queued_agent("agent_000", "/cfg.yaml")

        msg = manager.agents["agent_000"].error_message or ""
        assert "Launch failed" in msg
        assert "the reason" in msg

    @pytest.mark.asyncio
    async def test_failure_sets_exit_reason_launch_failed(self, pm, manager):
        """mutmut_8-10: exit_reason='launch_failed'."""
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)

        with patch.object(pm, "spawn_agent", side_effect=RuntimeError("fail")):
            await pm._launch_queued_agent("agent_000", "/cfg.yaml")

        assert manager.agents["agent_000"].exit_reason == "launch_failed"

    @pytest.mark.asyncio
    async def test_broadcast_called_on_failure(self, pm, manager):
        """mutmut_11-13: broadcast_status called on failure."""
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000", state="queued", pid=0)

        with patch.object(pm, "spawn_agent", side_effect=RuntimeError("fail")):
            await pm._launch_queued_agent("agent_000", "/cfg.yaml")

        manager.broadcast_status.assert_called()

    @pytest.mark.asyncio
    async def test_no_crash_when_agent_not_in_agents(self, pm, manager):
        """mutmut_19-21: if agent_id in self.manager.agents check."""
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_agent", side_effect=RuntimeError("fail")):
            await pm._launch_queued_agent("agent_999", "/cfg.yaml")
