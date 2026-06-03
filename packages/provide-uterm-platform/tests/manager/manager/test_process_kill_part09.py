#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — StopProcessTree2."""

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


class TestStopProcessTreeKills2:
    """Kill-tests for AgentProcessManager._stop_process_tree.

    The method resolves ``os``/``signal``/``logger``/``contextlib`` from the
    ``process_impl`` module globals, so all patching targets ``process_impl``.
    """

    # --- guard: resolved_pid <= 0 ------------------------------------------
    async def test_none_nt_suppress_second_none_raises_on_runtime(self, pm):
        """mutmut_13: none-nt suppress (OSError, None); RuntimeError -> TypeError on mutant."""
        import provide.uterm.manager.process_impl as pi

        log = MagicMock()
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_taskkill_process_tree", AsyncMock(side_effect=RuntimeError("x"))),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=None, pid=31)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_none_nt_suppress_drop_runtime_propagates(self, pm):
        """mutmut_15: none-nt suppress (OSError,) no longer catches RuntimeError."""
        import provide.uterm.manager.process_impl as pi

        log = MagicMock()
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_taskkill_process_tree", AsyncMock(side_effect=RuntimeError("x"))),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=None, pid=32)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    # --- final force-kill section (after first wait times out) -------------
    async def test_final_posix_sigkill_args(self, pm):
        """mutmut_64: final _signal_posix_process_group(resolved_pid, SIGKILL)."""
        from unittest.mock import call

        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=701)
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, None])),
            patch.object(pm, "_signal_posix_process_group") as sig,
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        assert sig.call_args_list == [
            call(701, pi.signal.SIGTERM),
            call(701, pi.signal.SIGKILL),
        ]

    async def test_final_nt_skips_posix_sigkill(self, pm):
        """mutmut_58/59: `if os.name != "nt"` guard; on nt the final SIGKILL must be skipped."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=702)
        with (
            patch.object(pi.os, "name", "nt"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, None])),
            patch.object(pm, "_taskkill_process_tree", new_callable=AsyncMock),
            patch.object(pm, "_signal_posix_process_group") as sig,
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        sig.assert_not_called()

    async def test_final_posix_suppress_first_none(self, pm):
        """mutmut_60: final suppress (None, ProcessLookupError); SIGKILL OSError -> TypeError mutant."""
        import provide.uterm.manager.process_impl as pi

        def sig_side(pid, s):
            if s == pi.signal.SIGKILL:
                raise OSError("eperm")

        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, None])),
            patch.object(pm, "_signal_posix_process_group", side_effect=sig_side),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=make_mock_proc(pid=701))
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_posix_suppress_second_none_propagates_runtime(self, pm):
        """mutmut_61: final suppress (OSError, None); SIGKILL RuntimeError -> TypeError mutant."""
        import provide.uterm.manager.process_impl as pi

        def sig_side(pid, s):
            if s == pi.signal.SIGKILL:
                raise RuntimeError("boom")

        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, None])),
            patch.object(pm, "_signal_posix_process_group", side_effect=sig_side),
        ):
            with pytest.raises(RuntimeError):
                await pm._stop_process_tree(agent_id="a", process=make_mock_proc(pid=701))

    async def test_final_posix_suppress_drop_oserror_propagates(self, pm):
        """mutmut_62: final suppress (ProcessLookupError,) no longer catches plain OSError."""
        import provide.uterm.manager.process_impl as pi

        def sig_side(pid, s):
            if s == pi.signal.SIGKILL:
                raise OSError("eperm")

        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, None])),
            patch.object(pm, "_signal_posix_process_group", side_effect=sig_side),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=make_mock_proc(pid=701))
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_wait_args(self, pm):
        """mutmut_74/75/78: final _wait_for_process_exit(process, 1.0)."""
        from unittest.mock import call

        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=703)
        wait = AsyncMock(side_effect=[TimeoutError, None])
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", wait),
            patch.object(pm, "_signal_posix_process_group"),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc, timeout_s=5.0)
        assert wait.await_args_list[-1] == call(proc, 1.0)

    async def test_final_suppress_first_none(self, pm):
        """mutmut_68: final-wait suppress (None, OSError, RuntimeError); 2nd wait TimeoutError -> TypeError mutant."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=704)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, TimeoutError])),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_suppress_middle_none(self, pm):
        """mutmut_69: final-wait suppress (TimeoutError, None, RuntimeError); 2nd wait RuntimeError -> TypeError mutant."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=705)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, RuntimeError("x")])),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_suppress_last_none(self, pm):
        """mutmut_70: final-wait suppress (TimeoutError, OSError, None); 2nd wait RuntimeError -> TypeError mutant."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=706)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, RuntimeError("x")])),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_suppress_drop_oserror_propagates(self, pm):
        """mutmut_72: final-wait suppress (TimeoutError, RuntimeError) no longer catches plain OSError."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=707)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, OSError("eio")])),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_suppress_drop_runtime_propagates(self, pm):
        """mutmut_73: final-wait suppress (TimeoutError, OSError,) no longer catches RuntimeError."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=708)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, RuntimeError("x")])),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="a", process=proc)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="a")

    async def test_final_force_killed_warning_logged(self, pm):
        """mutmut_79/80/82/83/84: final logger.warning('agent_force_killed', agent_id=...)."""
        import provide.uterm.manager.process_impl as pi

        proc = make_mock_proc(pid=709)
        log = MagicMock()
        with (
            patch.object(pi.os, "name", "posix"),
            patch.object(pm, "_wait_for_process_exit", AsyncMock(side_effect=[TimeoutError, None])),
            patch.object(pm, "_signal_posix_process_group"),
            patch.object(pi, "logger", log),
        ):
            await pm._stop_process_tree(agent_id="ff", process=proc)
        log.warning.assert_called_once_with("agent_force_killed", agent_id="ff")


# ---- spawn_agent (45 killed, 0 equiv) ----
