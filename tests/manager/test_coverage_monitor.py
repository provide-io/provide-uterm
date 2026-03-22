#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for _monitor.py and process.py missing branches."""

from __future__ import annotations

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.manager._monitor import (
    _handle_desired_state,
    _handle_exited_processes,
)
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
        pass


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
    manager.broadcast_status = AsyncMock()
    return pm


def make_mock_proc(pid=42, returncode=0):
    m = MagicMock()
    m.pid = pid
    m.returncode = returncode
    m.poll.return_value = None
    m.wait.return_value = returncode
    return m


class TestHandleExitedProcessesExistingExitReason:
    """Cover _monitor.py line 42->57: exit_reason already set, exit_code==0, state==error."""

    @pytest.mark.asyncio
    async def test_exit_reason_not_overwritten_when_already_set(self, pm, manager):
        """When bot has exit_reason already set and exits with code 0, don't overwrite it."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        manager.processes["bot_000"] = proc
        manager.bots["bot_000"] = BotStatusBase(
            bot_id="bot_000",
            state="error",
            exit_reason="heartbeat_timeout",  # already set
        )

        await _handle_exited_processes(pm)

        # exit_reason should NOT be overwritten with "reported_error_then_exit_0"
        assert manager.bots["bot_000"].exit_reason == "heartbeat_timeout"
        assert manager.bots["bot_000"].state == "error"


class TestHandleDesiredStateAlreadyRegistered:
    """Cover _monitor.py branch 180->187: allocate_bot_id returns ID already in bots dict."""

    @pytest.mark.asyncio
    async def test_skip_register_when_bot_already_in_bots(self, pm, manager):
        """When allocate_bot_id returns an ID already in bots, skip the bots[new_bot_id] = ... block.

        desired_bots=2, one queued bot → active_count=1, deficit=1.
        allocate_bot_id returns the existing bot ID → if condition is False → line 187 reached.
        """
        manager.desired_bots = 2
        pm._last_spawn_config = "/some/config.yaml"

        # Pre-populate bots with the ID that allocate_bot_id will return
        pre_existing_bot = BotStatusBase(
            bot_id="bot_000",
            state="queued",
            config="/some/config.yaml",
        )
        manager.bots["bot_000"] = pre_existing_bot

        # Patch allocate_bot_id to return bot_000 (already in bots)
        with (
            patch.object(pm, "allocate_bot_id", return_value="bot_000"),
            patch.object(pm, "_launch_queued_bot", new_callable=AsyncMock),
            patch("provide.terminal.manager._monitor.asyncio.create_task", return_value=asyncio.Future()),
        ):
            await _handle_desired_state(pm)

        # The pre-existing bot object should remain unchanged (not replaced)
        assert manager.bots.get("bot_000") is pre_existing_bot

    @pytest.mark.asyncio
    async def test_branch_180_false_skips_bot_creation(self, pm, manager):
        """Branch 180->187 (False): deficit>0, allocate_bot_id returns ID already in bots.

        Setup: desired=3, two active bots → deficit=1. allocate_bot_id returns "bot_001"
        which is ALREADY in bots (active), so the `if new_bot_id not in pm.manager.bots:`
        block (lines 181-186) is False/skipped and execution goes to line 187 (logger.info).
        """
        manager.desired_bots = 3
        pm._last_spawn_config = "/some/config.yaml"

        # Two active bots → active_count=2, deficit=3-2=1
        manager.bots["bot_000"] = BotStatusBase(bot_id="bot_000", state="running", config="/some/config.yaml")
        pre_existing = BotStatusBase(bot_id="bot_001", state="running", config="/some/config.yaml")
        manager.bots["bot_001"] = pre_existing

        created_tasks: list[asyncio.Task] = []

        def _capture_task(coro):
            task = asyncio.get_event_loop().create_task(coro)
            created_tasks.append(task)
            return task

        # allocate_bot_id returns "bot_001" which IS already in bots → if False → skip block
        with (
            patch.object(pm, "allocate_bot_id", return_value="bot_001"),
            patch("provide.terminal.manager._monitor.asyncio.create_task", side_effect=_capture_task),
        ):
            await _handle_desired_state(pm)

        # asyncio.create_task was called (line 193 is reached), so branch 187+ was hit
        assert len(created_tasks) == 1
        # Cancel the task to avoid "Task was destroyed but it is pending" warnings
        created_tasks[0].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await created_tasks[0]

        # pre-existing bot object was not replaced (if block on line 180 was False)
        assert manager.bots.get("bot_001") is pre_existing


class TestSpawnBotNameStyleFalseBranch:
    """Cover process.py line 192->194: _spawn_name_style falsy, _spawn_name_base truthy."""

    @pytest.mark.asyncio
    async def test_spawn_bot_no_style_but_has_base(self, pm, manager, tmp_path):
        """When _spawn_name_style is empty string but _spawn_name_base is set,
        only NAME_BASE env var is injected (not NAME_STYLE)."""
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")

        pm._spawn_name_style = ""  # falsy → skip NAME_STYLE injection
        pm._spawn_name_base = "mybase"  # truthy → inject NAME_BASE

        captured_env: dict = {}

        def capture_spawn(bot_id, cmd, env):
            captured_env.update(env)
            return MagicMock(pid=789)

        with patch.object(pm, "_spawn_process", side_effect=capture_spawn):
            await pm.spawn_bot(str(config), "bot_000")

        prefix = manager.config.worker_env_prefix
        assert f"{prefix}NAME_STYLE" not in captured_env
        assert captured_env.get(f"{prefix}NAME_BASE") == "mybase"


class TestWaitForProcessExitAwaitable:
    """Cover process.py line 264: isawaitable(result) is True path."""

    @pytest.mark.asyncio
    async def test_awaitable_result_is_awaited(self):
        """When run_in_executor returns an awaitable, the inner await is executed."""
        proc = MagicMock()
        awaited = []

        async def fake_awaitable():
            awaited.append(True)

        coro = fake_awaitable()

        # Make run_in_executor return an awaitable (coroutine)
        # so inspect.isawaitable(result) is True
        async def fake_run_in_executor(executor, fn):
            return coro

        loop = asyncio.get_running_loop()
        with (
            patch.object(loop, "run_in_executor", side_effect=fake_run_in_executor),
            patch("provide.terminal.manager.process.inspect.isawaitable", return_value=True),
        ):
            await BotProcessManager._wait_for_process_exit(proc, 5.0)

        # The inner awaitable (coro) should have been awaited
        assert awaited == [True]


class TestTaskkillProcessTree:
    """Cover process.py lines 273-282: _taskkill_process_tree method."""

    @pytest.mark.asyncio
    async def test_taskkill_calls_subprocess_exec(self):
        """_taskkill_process_tree calls asyncio.create_subprocess_exec with taskkill args."""
        fake_proc = MagicMock()
        fake_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = fake_proc
            await BotProcessManager._taskkill_process_tree(1234)

        mock_exec.assert_awaited_once_with(
            "taskkill",
            "/PID",
            "1234",
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        fake_proc.wait.assert_awaited_once()


class TestStopProcessTreeNoProcess:
    """Cover process.py lines 297-304: process is None branch on POSIX."""

    @pytest.mark.asyncio
    async def test_posix_no_process_sends_sigkill_to_group(self, pm):
        """On non-Windows, process=None path calls _signal_posix_process_group with SIGKILL."""
        if os.name == "nt":
            pytest.skip("POSIX-only test")

        with patch.object(
            BotProcessManager,
            "_signal_posix_process_group",
        ) as mock_signal:
            await pm._stop_process_tree(bot_id="bot_test", pid=99999, process=None)

        mock_signal.assert_called_once()
        args = mock_signal.call_args[0]
        assert args[0] == 99999

    @pytest.mark.asyncio
    async def test_posix_no_process_suppresses_os_error(self, pm):
        """OSError from _signal_posix_process_group is suppressed when process=None."""
        if os.name == "nt":
            pytest.skip("POSIX-only test")

        with patch.object(
            BotProcessManager,
            "_signal_posix_process_group",
            side_effect=OSError("no such process"),
        ):
            # Should not raise
            await pm._stop_process_tree(bot_id="bot_test", pid=99999, process=None)


class TestStopProcessTreeSigkillAfterTimeout:
    """Cover process.py line 322->325: SIGKILL after SIGTERM timeout."""

    @pytest.mark.asyncio
    async def test_sigkill_sent_after_sigterm_timeout(self, pm):
        """When _wait_for_process_exit raises TimeoutError on first call, SIGKILL is sent."""
        if os.name == "nt":
            pytest.skip("POSIX-only test")

        proc = MagicMock()
        proc.pid = 99998

        call_count = 0

        async def fake_wait(p, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("first timeout")
            # second call succeeds

        with (
            patch.object(BotProcessManager, "_wait_for_process_exit", side_effect=fake_wait),
            patch.object(BotProcessManager, "_signal_posix_process_group") as mock_signal,
        ):
            await pm._stop_process_tree(bot_id="bot_000", process=proc, timeout_s=0.01)

        # _signal_posix_process_group called at least twice:
        # once for SIGTERM, once for SIGKILL
        assert mock_signal.call_count >= 2
        import signal

        signal_args = [call[0][1] for call in mock_signal.call_args_list]
        assert signal.SIGKILL in signal_args


class TestSpawnSwarmBotAlreadyRegistered:
    """Cover process.py line 349->347: bot_id already in manager.bots."""

    @pytest.mark.asyncio
    async def test_spawn_swarm_skips_preregistered_bots(self, pm, manager, tmp_path):
        """When bot IDs that spawn_swarm would allocate are already in bots,
        the 'if bot_id not in manager.bots' check is False and the block is skipped.

        We patch sync_next_bot_index() to return 0, and pre-populate "bot_000",
        so the pre-registration loop sees bot_000 already in bots.
        """
        config = tmp_path / "c.yaml"
        config.write_text("worker_type: test_game\n")

        # Pre-populate bot_000 so spawn_swarm sees it already registered
        original_bot = BotStatusBase(
            bot_id="bot_000",
            state="running",
            config=str(config),
        )
        manager.bots["bot_000"] = original_bot

        # Patch sync_next_bot_index to return 0 so base_index=0 and bot_id="bot_000"
        with (
            patch.object(pm, "sync_next_bot_index", return_value=0),
            patch.object(pm, "spawn_bot", new_callable=AsyncMock, return_value="bot_000"),
        ):
            manager.broadcast_status = AsyncMock()
            await pm.spawn_swarm([str(config)], group_size=1, group_delay=0)

        # The pre-existing bot entry should not be replaced with a "queued" one
        # (the if block was False, so bots["bot_000"] was not overwritten)
        assert manager.bots.get("bot_000") is original_bot
