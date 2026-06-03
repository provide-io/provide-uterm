#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — StopProcessTree."""

from __future__ import annotations

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


class TestStopProcessTreeKills:
    """Kill-tests for AgentProcessManager._stop_process_tree.

    The method resolves ``os``/``signal``/``logger``/``contextlib`` from the
    ``process_impl`` module globals, so all patching targets ``process_impl``.
    """

    # --- guard: resolved_pid <= 0 ------------------------------------------
    async def test_zero_pid_returns_without_signalling(self, pm):
        """mutmut_6: `<= 0` -> `< 0` would let pid==0 fall through and signal."""
        with patch.object(pm, "_signal_posix_process_group") as sig:
            await pm._stop_process_tree(agent_id="a", process=None, pid=0)
        sig.assert_not_called()

    async def test_pid_one_is_signalled(self, pm):
        """mutmut_7: `<= 0` -> `<= 1` would early-return for pid==1."""
        import provide.uterm.manager.process_impl as pi

        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_signal_posix_process_group") as sig,
        ):
            await pm._stop_process_tree(agent_id="a", process=None, pid=1)
        sig.assert_called_once_with(1, pi.signal.SIGKILL)

    # --- process is None branch: posix force-kill --------------------------
    async def test_none_branch_posix_sigkill_args(self, pm):
        """mutmut_22/24: SIGKILL arg dropped/None in process=None branch."""
        import provide.uterm.manager.process_impl as pi

        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_signal_posix_process_group") as sig,
        ):
            await pm._stop_process_tree(agent_id="a", process=None, pid=555)
        sig.assert_called_once_with(555, pi.signal.SIGKILL)

    async def test_none_branch_posix_suppress_oserror_only_propagates_runtime(self, pm):
        """mutmut_18: suppress(OSError, ProcessLookupError) -> (OSError, None).

        Raising RuntimeError: original propagates RuntimeError; the (OSError, None)
        mutant raises TypeError from isinstance instead.
        """
        import provide.uterm.manager.process_impl as pi

        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_signal_posix_process_group", side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError):
                await pm._stop_process_tree(agent_id="a", process=None, pid=7)

    async def test_none_branch_warning_logged(self, pm):
        """mutmut_25/26/28/29/30: logger.warning('agent_force_killed', agent_id=...)."""
        import provide.uterm.manager.process_impl as pi

        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="zz", process=None, pid=33)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="zz")

    # --- process present: posix SIGTERM path -------------------------------
    async def test_proc_posix_sigterm_args(self, pm):
        """mutmut_43/44: SIGTERM call uses (resolved_pid, signal.SIGTERM)."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=888)
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock),
            patch.object(pm, "_signal_posix_process_group") as sig,
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        sig.assert_called_once_with(888, pi.signal.SIGTERM)

    async def test_proc_posix_suppress_none_propagates_runtime(self, pm):
        """mutmut_40: site-D suppress (OSError, None); RuntimeError -> TypeError on mutant."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=12)
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock),
            patch.object(pm, "_signal_posix_process_group", side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError):
                await pm._stop_process_tree(agent_id="a", process=proc)

    async def test_proc_posix_suppress_first_none_raises_typeerror_on_mutant(self, pm):
        """mutmut_39: site-D suppress (None, ProcessLookupError).

        Original suppresses OSError and reaches the wait; the (None, ...) mutant
        raises TypeError so the wait is never awaited.
        """
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=13)
        wait = AsyncMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_signal_posix_process_group", side_effect=OSError("eperm")),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        wait.assert_awaited()

    async def test_proc_posix_suppress_drop_oserror_propagates(self, pm):
        """mutmut_41: site-D suppress (ProcessLookupError,) no longer catches plain OSError."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=14)
        wait = AsyncMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_signal_posix_process_group", side_effect=OSError("eperm")),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        wait.assert_awaited()

    async def test_proc_terminated_info_logged(self, pm):
        """mutmut_51/52/54/55/56: logger.info('agent_terminated', agent_id=...)."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=15)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="term1", process=proc)
        log.info.assert_called_once_with("agent_terminated", agent_id="term1")

    # --- process present: Windows (nt) taskkill path -----------------------
    async def test_proc_nt_taskkill_args(self, pm):
        """mutmut_38: proc-nt _taskkill_process_tree(resolved_pid) -> (None)."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=987)
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", new_callable=AsyncMock),
            patch.object(pm, "_taskkill_process_tree", new_callable=AsyncMock) as tk,
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        tk.assert_awaited_once_with(987)

    async def test_proc_nt_suppress_first_none_raises_on_oserror(self, pm):
        """mutmut_34: proc-nt suppress (None, RuntimeError); OSError -> TypeError on mutant."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=21)
        wait = AsyncMock()
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_taskkill_process_tree", AsyncMock(side_effect=OSError("x"))),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        wait.assert_awaited()

    async def test_proc_nt_suppress_second_none_raises_on_runtime(self, pm):
        """mutmut_35: proc-nt suppress (OSError, None); RuntimeError -> TypeError on mutant."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=22)
        wait = AsyncMock()
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_taskkill_process_tree", AsyncMock(side_effect=RuntimeError("x"))),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        wait.assert_awaited()

    async def test_proc_nt_suppress_drop_oserror_propagates(self, pm):
        """mutmut_36: proc-nt suppress (RuntimeError,) no longer catches OSError."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=23)
        wait = AsyncMock()
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_taskkill_process_tree", AsyncMock(side_effect=OSError("x"))),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        wait.assert_awaited()

    async def test_proc_nt_suppress_drop_runtime_propagates(self, pm):
        """mutmut_37: proc-nt suppress (OSError,) no longer catches RuntimeError."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=24)
        wait = AsyncMock()
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_taskkill_process_tree", AsyncMock(side_effect=RuntimeError("x"))),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        wait.assert_awaited()

    # --- process is None: Windows (nt) taskkill path -----------------------
