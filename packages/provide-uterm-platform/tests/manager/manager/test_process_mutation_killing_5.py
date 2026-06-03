#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.uterm.manager.process_impl — survivor batch (part 5).

Targets the 296 mutants that the existing test_process_*.py / test_coverage_*.py suites
left surviving (the late async/platform methods: _stop_process_tree, spawn_agent,
_build_preexec_rlimit_fn, spawn_swarm, _spawn_platform_kwargs, _load_worker_type,
kill_agent, release_agent_account, ...). The autouse conftest blanket-mocks
subprocess.Popen / os.killpg / os.getpgid during mutation runs (keyed on
MUTANT_UNDER_TEST), so a guard-defeat mutant can never spawn a real child.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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


def make_mock_proc(pid=42, returncode=0):
    m = MagicMock()
    m.pid = pid
    m.returncode = returncode
    m.poll.return_value = None
    m.wait.return_value = returncode
    return m


@contextlib.contextmanager
def patched_environ(fake_env):
    """Replace ``process_impl.os.environ`` with ``fake_env`` for _build_worker_env
    tests, but KEEP ``MUTANT_UNDER_TEST``. ``process_impl.os`` is the shared ``os``
    module, so patching its ``environ`` to a dict missing ``MUTANT_UNDER_TEST`` would
    make the mutmut trampoline's ``os.environ['MUTANT_UNDER_TEST']`` raise KeyError
    (breaking every mutation run). Preserving it keeps the trampoline working while
    still giving _build_worker_env the controlled base env the test wants.
    """
    env = dict(fake_env)
    mut = os.environ.get("MUTANT_UNDER_TEST")
    if mut is not None:
        env["MUTANT_UNDER_TEST"] = mut
    with patch("provide.uterm.manager.process_impl.os.environ", env):
        yield env


# ==========================================================================
# Per-method kill classes (integrated from the workflow agents) appear below.
# ==========================================================================


# ---- _stop_process_tree (46 killed, 4 equiv) ----
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
class TestSpawnAgentKills:
    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _cfg(tmp_path, text="worker_type: test_game\n"):
        p = tmp_path / "cfg.yaml"
        p.write_text(text)
        return str(p)

    # ---- guard clauses -------------------------------------------------
    @pytest.mark.asyncio
    async def test_note_agent_id_called_with_real_id(self, pm, manager, tmp_path):
        """m1: note_agent_id(None) -> _next_agent_index not advanced."""
        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_agent(cfg, "agent_005")
        # note_agent_id(agent_005) must bump _next_agent_index to >= 6.
        assert pm._next_agent_index >= 6

    @pytest.mark.asyncio
    async def test_max_agents_uses_ge_not_gt(self, pm, manager, tmp_path):
        """m2: >= -> > would allow one over the configured limit."""
        from provide.uterm.manager.models import AgentStatusBase

        manager.max_agents = 1
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        cfg = self._cfg(tmp_path)
        with pytest.raises(RuntimeError, match="Max agents"):
            await pm.spawn_agent(cfg, "agent_001")

    @pytest.mark.asyncio
    async def test_max_agents_error_message_preserved(self, pm, manager, tmp_path):
        """m3: RuntimeError(None) -> message becomes 'None'."""
        from provide.uterm.manager.models import AgentStatusBase

        manager.max_agents = 1
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        cfg = self._cfg(tmp_path)
        with pytest.raises(RuntimeError, match=r"Max agents \(1\) reached"):
            await pm.spawn_agent(cfg, "agent_001")

    @pytest.mark.asyncio
    async def test_config_not_found_error_message_preserved(self, pm, manager):
        """m6: RuntimeError(None) -> message becomes 'None'."""
        with pytest.raises(RuntimeError, match="Config not found: /does/not/exist.yaml"):
            await pm.spawn_agent("/does/not/exist.yaml", "agent_000")

    # ---- logger.info("spawning_agent", ...) ----------------------------
    @pytest.mark.asyncio
    async def test_spawning_agent_info_log_args(self, pm, manager, tmp_path):
        """m7/8/9/11/12/13/14: logger.info first positional + agent_id/config_path kwargs."""
        import provide.uterm.manager.process_impl as mod

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        fake_logger = MagicMock()
        with patch.object(mod, "logger", fake_logger):
            with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
                await pm.spawn_agent(cfg, "agent_009")

        spawning = [
            (c.args, c.kwargs) for c in fake_logger.info.call_args_list if c.args and c.args[0] == "spawning_agent"
        ]
        assert spawning, f"no spawning_agent info log: {fake_logger.info.call_args_list}"
        a, k = spawning[0]
        assert a[0] == "spawning_agent"
        assert k.get("agent_id") == "agent_009"
        assert k.get("config_path") == cfg

    # ---- _load_worker_type(config_path) --------------------------------
    @pytest.mark.asyncio
    async def test_load_worker_type_gets_config_path(self, pm, manager, tmp_path):
        """m16: _load_worker_type(None) -> read fails -> worker_type 'default'.

        With a second registry entry the 'default' single-entry fallback no
        longer applies, so the mutant raises 'Unknown worker_type'. The
        original reads the real config_path, resolves 'test_game', succeeds.
        """
        cfg = self._cfg(tmp_path, "worker_type: test_game\n")
        pm._worker_registry["other"] = FakeWorkerPlugin()
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_agent(cfg, "agent_000")
        assert result == "agent_000"

    # ---- policy gate intercept_spawn(agent_id, config_path, raw) -------
    @pytest.mark.asyncio
    async def test_policy_gate_receives_all_three_args(self, pm, manager, tmp_path):
        """m18/19/20: intercept_spawn positional args agent_id/config_path/raw."""
        cfg = self._cfg(tmp_path, "worker_type: test_game\nfoo: bar\n")
        manager.broadcast_status = AsyncMock()
        seen = {}

        class RecordingGate:
            async def intercept_spawn(self, agent_id, config_path, raw):
                seen["agent_id"] = agent_id
                seen["config_path"] = config_path
                seen["raw"] = raw
                return True

        pm.set_policy_gate(RecordingGate())
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_agent(cfg, "agent_003")
        assert seen["agent_id"] == "agent_003"
        assert seen["config_path"] == cfg
        assert seen["raw"] == {"worker_type": "test_game", "foo": "bar"}

    # ---- logger.warning on policy reject -------------------------------
    @pytest.mark.asyncio
    async def test_policy_reject_warning_log_and_error(self, pm, manager, tmp_path):
        """m24/25/27/28/29: logger.warning('agent_spawn_rejected_by_policy', agent_id=...)."""
        import provide.uterm.manager.process_impl as mod

        cfg = self._cfg(tmp_path)

        class DenyGate:
            async def intercept_spawn(self, agent_id, config_path, raw):
                return False

        pm.set_policy_gate(DenyGate())
        fake_logger = MagicMock()
        with patch.object(mod, "logger", fake_logger):
            with pytest.raises(RuntimeError, match="Spawn rejected by policy for agent agent_003"):
                await pm.spawn_agent(cfg, "agent_003")

        rej = [
            (c.args, c.kwargs)
            for c in fake_logger.warning.call_args_list
            if c.args and c.args[0] == "agent_spawn_rejected_by_policy"
        ]
        assert rej, f"missing rejection warning: {fake_logger.warning.call_args_list}"
        assert rej[0][1].get("agent_id") == "agent_003"

    # ---- _get_registry_entry(worker_type, config_path) ----------------
    @pytest.mark.asyncio
    async def test_registry_error_message_includes_config_path(self, pm, manager, tmp_path):
        """m33: _get_registry_entry(worker_type, None) -> config_path dropped from error msg."""
        cfg = self._cfg(tmp_path, "worker_type: not_registered\n")
        pm._worker_registry["other"] = FakeWorkerPlugin()
        with pytest.raises(RuntimeError, match=r"not_registered"):
            await pm.spawn_agent(cfg, "agent_000")
        # The config_path must appear in the message (None mutation drops it).
        try:
            await pm.spawn_agent(cfg, "agent_000")
        except RuntimeError as e:
            assert cfg in str(e)

    # ---- worker_module + cmd list --------------------------------------
    @pytest.mark.asyncio
    async def test_cmd_exact_contents(self, pm, manager, tmp_path):
        """m36/38/39/40/41/42/43: worker_module=None & cmd string literals."""
        import sys

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["cmd"] = list(cmd)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(cfg, "agent_007")

        assert captured["cmd"] == [
            sys.executable,
            "-m",
            "test_module",
            "--config",
            cfg,
            "--agent-id",
            "agent_007",
        ]

    # ---- agent_entry resolution + _build_worker_env --------------------
    @pytest.mark.asyncio
    async def test_agent_entry_passed_so_configure_worker_env_runs(self, pm, manager, tmp_path):
        """m45/46/49: agent_entry None -> configure_worker_env skipped -> env lacks CONFIGURED."""
        from provide.uterm.manager.models import AgentStatusBase

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        # Pre-register the agent so agents.get(agent_id) is truthy.
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["env"] = dict(env)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(cfg, "agent_000")

        assert captured["env"].get("CONFIGURED") == "yes"

    @pytest.mark.asyncio
    async def test_raw_config_forwarded_to_configure_worker_env(self, pm, manager, tmp_path):
        """m51: _build_worker_env(..., raw->None, ...) drops raw_config kwarg."""
        from provide.uterm.manager.models import AgentStatusBase

        cfg = self._cfg(tmp_path, "worker_type: test_game\nmagic: 7\n")
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")

        class RawRecordingPlugin(FakeWorkerPlugin):
            def configure_worker_env(self, env, agent_status, manager, **kwargs):
                raw = kwargs.get("raw_config")
                env["RAW_MAGIC"] = str(raw.get("magic")) if raw else "MISSING"

        pm._worker_registry["test_game"] = RawRecordingPlugin()
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["env"] = dict(env)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(cfg, "agent_000")

        assert captured["env"].get("RAW_MAGIC") == "7"

    @pytest.mark.asyncio
    async def test_agent_id_forwarded_to_build_worker_env(self, pm, manager, tmp_path):
        """m52: _build_worker_env(..., agent_id->None) breaks token derivation."""
        import os

        from provide.uterm.manager.auth import derive_agent_token

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        operator_var = manager.config.auth_token_env_var
        worker_var = manager.config.auth_worker_token_env_var
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["env"] = dict(env)
            return make_mock_proc()

        expected = derive_agent_token("fleet-secret", "agent_000")
        with patch.dict(os.environ, {worker_var: "fleet-secret"}, clear=False):
            with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
                await pm.spawn_agent(cfg, "agent_000")

        assert captured["env"].get(operator_var) == expected

    # ---- update-branch state writes ------------------------------------
    @pytest.mark.asyncio
    async def test_update_branch_records_pid(self, pm, manager, tmp_path):
        """m68: update-branch pid=None."""
        from provide.uterm.manager.models import AgentStatusBase

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc(pid=4242)):
            await pm.spawn_agent(cfg, "agent_000")
        assert manager.agents["agent_000"].pid == 4242
        assert manager.agents["agent_000"].state == "running"

    # ---- insert-branch (no pre-existing agent) -------------------------
    @pytest.mark.asyncio
    async def test_insert_branch_records_pid(self, pm, manager, tmp_path):
        """m77/83: insert-branch pid=None / pid arg dropped -> defaults to None."""
        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        assert "agent_000" not in manager.agents
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc(pid=4242)):
            await pm.spawn_agent(cfg, "agent_000")
        assert manager.agents["agent_000"].pid == 4242
        assert manager.agents["agent_000"].config == cfg

    @pytest.mark.asyncio
    async def test_insert_branch_seeds_last_update_time(self, pm, manager, tmp_path):
        """m87: insert-branch drops last_update_time arg -> defaults to 0.0."""
        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        assert "agent_000" not in manager.agents
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_agent(cfg, "agent_000")
        assert manager.agents["agent_000"].last_update_time > 0.0

    # ---- exception path: logger.exception("agent_spawn_failed", ...) ---
    @pytest.mark.asyncio
    async def test_spawn_failure_exception_log_and_raise(self, pm, manager, tmp_path):
        """m100/101/102/104/105/106/107/108: logger.exception args + RuntimeError wrap."""
        import provide.uterm.manager.process_impl as mod

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        boom = ValueError("kaboom-unique-123")
        fake_logger = MagicMock()

        with patch.object(mod, "logger", fake_logger):
            with patch.object(pm, "_spawn_process", side_effect=boom):
                with pytest.raises(RuntimeError, match="Failed to spawn agent: kaboom-unique-123"):
                    await pm.spawn_agent(cfg, "agent_011")

        failed = [
            (c.args, c.kwargs)
            for c in fake_logger.exception.call_args_list
            if c.args and c.args[0] == "agent_spawn_failed"
        ]
        assert failed, f"missing failure log: {fake_logger.exception.call_args_list}"
        a, k = failed[0]
        assert a[0] == "agent_spawn_failed"
        assert k.get("agent_id") == "agent_011"
        assert k.get("error") == "kaboom-unique-123"


# ---- _build_preexec_rlimit_fn (35 killed, 0 equiv) ----
class TestBuildPreexecRlimitFn:
    @staticmethod
    def _kinds():
        import resource

        return (int(resource.RLIMIT_NOFILE), int(resource.RLIMIT_AS), int(resource.RLIMIT_CPU))

    @staticmethod
    def _build_with(pm, *, nofile_soft=0, nofile_hard=0, as_mb=0, cpu_s=0):
        c = pm.manager.config
        c.worker_rlimit_nofile_soft = nofile_soft
        c.worker_rlimit_nofile_hard = nofile_hard
        c.worker_rlimit_as_mb = as_mb
        c.worker_rlimit_cpu_s = cpu_s
        return pm._build_preexec_rlimit_fn()

    @staticmethod
    def _collect(fn):
        recorded = {}

        def fake_setrlimit(kind, pair):
            recorded[kind] = pair

        with patch("resource.setrlimit", side_effect=fake_setrlimit):
            fn()
        return recorded

    # --- mut 2,3: os.name == "nt" guard ----------------------------------
    def test_nt_returns_none_even_with_limits_configured(self, pm):
        """mut_2/3: under os.name=='nt' the original returns None regardless of config."""
        with patch("provide.uterm.manager.process_impl.os") as fake_os:
            fake_os.name = "nt"
            fn = self._build_with(pm, nofile_soft=100, as_mb=64, cpu_s=10)
        assert fn is None

    def test_posix_with_limits_returns_callable(self, pm):
        """Sanity / guards mut_2/3 from the other side: posix yields a function."""
        import os

        with patch.object(os, "name", "posix"):
            fn = self._build_with(pm, nofile_soft=100)
        assert callable(fn)

    # --- mut 8: nofile_soft `or 0` -> `and 0` ----------------------------
    def test_nofile_soft_uses_configured_value(self, pm):
        """mut_8: `soft or 0` -> `soft and 0` zeroes a configured soft."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=256, nofile_hard=0)
        rec = self._collect(fn)
        assert rnofile in rec
        assert rec[rnofile] == (256, 256)

    # --- mut 13: nofile_hard `or 0` -> `or 1` ----------------------------
    def test_nofile_hard_zero_defaults_to_soft_not_one(self, pm):
        """mut_13: hard config 0 -> original 0 (then = soft); mutant -> 1."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=256, nofile_hard=0)
        rec = self._collect(fn)
        assert rec[rnofile] == (256, 256)

    # --- mut 14: `and hasattr` -> `or hasattr` ---------------------------
    def test_no_nofile_limits_skips_nofile_spec(self, pm):
        """mut_14: both nofile 0 -> original skips; mutant `or` enters and appends (0,0)."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=0, nofile_hard=0, as_mb=64)
        rec = self._collect(fn)
        assert rnofile not in rec

    # --- mut 16: nofile_soft `> 0` -> `>= 0` -----------------------------
    def test_nofile_soft_zero_does_not_trigger_via_soft(self, pm):
        """mut_16: soft 0, hard 0 -> original skips; mutant `>=0` enters."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=0, nofile_hard=0, cpu_s=5)
        rec = self._collect(fn)
        assert rnofile not in rec

    # --- mut 17: nofile_soft `> 0` -> `> 1` ------------------------------
    def test_nofile_soft_one_triggers_nofile(self, pm):
        """mut_17: soft 1, hard 0 -> original enters (1>0); mutant `>1` would not."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=1, nofile_hard=0)
        rec = self._collect(fn)
        assert rnofile in rec
        assert rec[rnofile] == (1, 1)

    # --- mut 18: nofile_hard `> 0` -> `>= 0` -----------------------------
    def test_nofile_hard_zero_does_not_trigger_via_hard(self, pm):
        """mut_18: hard 0, soft 0 -> original skips; mutant `>=0` enters."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=0, nofile_hard=0, cpu_s=5)
        rec = self._collect(fn)
        assert rnofile not in rec

    # --- mut 19: nofile_hard `> 0` -> `> 1` ------------------------------
    def test_nofile_hard_one_triggers_nofile(self, pm):
        """mut_19: hard 1, soft 0 -> original enters via hard; mutant `>1` would not."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=0, nofile_hard=1)
        rec = self._collect(fn)
        assert rnofile in rec
        assert rec[rnofile] == (1, 1)

    # --- mut 27: `nofile_soft <= 0` -> `<= 1` ----------------------------
    def test_nofile_soft_one_is_preserved(self, pm):
        """mut_27: soft 1, hard 9 -> original keeps soft=1; mutant `<=1` overwrites with 9."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=1, nofile_hard=9)
        rec = self._collect(fn)
        assert rec[rnofile] == (1, 9)

    # --- mut 30: `nofile_hard <= 0` -> `<= 1` ----------------------------
    def test_nofile_hard_one_is_preserved(self, pm):
        """mut_30: hard 1, soft 9 -> original keeps hard=1; mutant `<=1` overwrites with 9."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=9, nofile_hard=1)
        rec = self._collect(fn)
        assert rec[rnofile] == (9, 1)

    # --- mut 31: `nofile_hard = nofile_soft` -> `= None` -----------------
    def test_nofile_hard_filled_from_soft_not_none(self, pm):
        """mut_31: hard 0 -> original sets hard=soft; mutant sets hard=None."""
        rnofile = self._kinds()[0]
        fn = self._build_with(pm, nofile_soft=512, nofile_hard=0)
        rec = self._collect(fn)
        assert rec[rnofile] == (512, 512)
        assert rec[rnofile][1] is not None

    # --- mut 36: as_mb `or 0` -> `and 0` ---------------------------------
    def test_as_mb_uses_configured_value(self, pm):
        """mut_36: `as_mb or 0` -> `as_mb and 0` zeroes as_mb -> AS branch skipped."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=64)
        rec = self._collect(fn)
        assert ras in rec
        assert rec[ras] == (64 * 1024 * 1024, 64 * 1024 * 1024)

    # --- mut 37: as_mb `or 0` -> `or 1` ----------------------------------
    def test_as_mb_zero_skips_as_branch(self, pm):
        """mut_37: as_mb 0 -> original 0 (skip); mutant `or 1` -> as_mb 1 (enter)."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=0, cpu_s=5)
        rec = self._collect(fn)
        assert ras not in rec

    # --- mut 38: `as_mb > 0 and` -> `or` ---------------------------------
    def test_as_mb_zero_and_hasattr_skips(self, pm):
        """mut_38: as_mb 0 but RLIMIT_AS exists -> original `and` skips; mutant `or` enters."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=0, cpu_s=5)
        rec = self._collect(fn)
        assert ras not in rec

    # --- mut 39: as_mb `> 0` -> `>= 0` -----------------------------------
    def test_as_mb_zero_does_not_enter(self, pm):
        """mut_39: as_mb 0 -> original skip; mutant `>=0` enters."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=0, cpu_s=5)
        rec = self._collect(fn)
        assert ras not in rec

    # --- mut 40: as_mb `> 0` -> `> 1` ------------------------------------
    def test_as_mb_one_enters_as_branch(self, pm):
        """mut_40: as_mb 1 -> original enters; mutant `>1` skips."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=1)
        rec = self._collect(fn)
        assert ras in rec
        assert rec[ras] == (1024 * 1024, 1024 * 1024)

    # --- mut 41/45/46: hasattr(resource, "RLIMIT_AS") target/name mutated -
    def test_as_branch_uses_correct_attr_lookup(self, pm):
        """mut_41 (hasattr(None,...)), mut_45/46 (bad attr name) -> AS spec dropped."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=16)
        rec = self._collect(fn)
        assert ras in rec
        assert rec[ras] == (16 * 1024 * 1024, 16 * 1024 * 1024)

    # --- mut 47: `as_bytes = None` ---------------------------------------
    def test_as_bytes_is_computed_not_none(self, pm):
        """mut_47: as_bytes=None -> setrlimit receives (None, None)."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=32)
        rec = self._collect(fn)
        assert rec[ras] == (32 * 1024 * 1024, 32 * 1024 * 1024)
        assert rec[ras][0] is not None

    # --- mut 48/49/50/51: `* 1024 * 1024` arithmetic mutated -------------
    def test_as_bytes_is_megabytes(self, pm):
        """mut_48/49 collapse to as_mb (float); mut_50/51 use 1025 -> wrong product."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=10)
        rec = self._collect(fn)
        assert rec[ras] == (10 * 1024 * 1024, 10 * 1024 * 1024)
        assert rec[ras][0] == 10485760
        assert rec[ras][0] != 10

    def test_as_bytes_factors_are_both_1024(self, pm):
        """mut_50 (1025*1024) / mut_51 (1024*1025) -> wrong product with a different as_mb."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=7)
        rec = self._collect(fn)
        assert rec[ras][0] == 7 * 1024 * 1024

    # --- mut 52: AS limit_specs.append(None) -----------------------------
    def test_as_spec_is_tuple_not_none(self, pm):
        """mut_52: append(None) -> closure unpacks `for k,s,h in specs` -> TypeError on call."""
        ras = self._kinds()[1]
        fn = self._build_with(pm, as_mb=8)
        rec = self._collect(fn)  # raises TypeError under the mutant
        assert rec[ras] == (8 * 1024 * 1024, 8 * 1024 * 1024)

    # --- mut 56: cpu_s `or 0` -> `and 0` ---------------------------------
    def test_cpu_uses_configured_value(self, pm):
        """mut_56: `cpu_s or 0` -> `cpu_s and 0` zeroes cpu_s -> CPU branch skipped."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=30)
        rec = self._collect(fn)
        assert rcpu in rec
        assert rec[rcpu] == (30, 30)

    # --- mut 57: cpu_s `or 0` -> `or 1` ----------------------------------
    def test_cpu_zero_skips_cpu_branch(self, pm):
        """mut_57: cpu_s 0 -> original 0 (skip); mutant `or 1` enters."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=0, as_mb=8)
        rec = self._collect(fn)
        assert rcpu not in rec

    # --- mut 58: `cpu_s > 0 and` -> `or` ---------------------------------
    def test_cpu_zero_and_hasattr_skips(self, pm):
        """mut_58: cpu_s 0 but RLIMIT_CPU exists -> original `and` skips; mutant `or` enters."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=0, as_mb=8)
        rec = self._collect(fn)
        assert rcpu not in rec

    # --- mut 59: cpu_s `> 0` -> `>= 0` -----------------------------------
    def test_cpu_zero_does_not_enter(self, pm):
        """mut_59: cpu_s 0 -> original skip; mutant `>=0` enters."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=0, as_mb=8)
        rec = self._collect(fn)
        assert rcpu not in rec

    # --- mut 60: cpu_s `> 0` -> `> 1` ------------------------------------
    def test_cpu_one_enters_cpu_branch(self, pm):
        """mut_60: cpu_s 1 -> original enters; mutant `>1` skips."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=1)
        rec = self._collect(fn)
        assert rcpu in rec
        assert rec[rcpu] == (1, 1)

    # --- mut 61/65/66: hasattr(resource, "RLIMIT_CPU") target/name mutated
    def test_cpu_branch_uses_correct_attr_lookup(self, pm):
        """mut_61 (hasattr(None,...)), mut_65/66 (bad attr name) -> CPU spec dropped."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=20)
        rec = self._collect(fn)
        assert rcpu in rec
        assert rec[rcpu] == (20, 20)

    # --- mut 67: CPU limit_specs.append(None) ----------------------------
    def test_cpu_spec_is_tuple_not_none(self, pm):
        """mut_67: append(None) -> closure unpacks -> TypeError on call."""
        rcpu = self._kinds()[2]
        fn = self._build_with(pm, cpu_s=15)
        rec = self._collect(fn)  # raises TypeError under the mutant
        assert rec[rcpu] == (15, 15)

    # ---- spawn_swarm (29 killed, 0 equiv) ----
    # --- default-argument mutants ------------------------------------------
    @pytest.mark.asyncio
    async def test_default_group_size_is_five(self, pm, manager, tmp_path):
        """mutmut_1: default group_size 5 -> 6. With 6 configs, default 5 = 2
        groups (one inter-group sleep); 6 = 1 group (no sleep)."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        sleeps = []

        async def fake_sleep(t):
            sleeps.append(t)

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"),
        ):
            # Do NOT pass group_size -> exercises the default. group_delay=0.0.
            await pm.spawn_swarm([str(cfg)] * 6, group_delay=0.0)

        # 6 configs / group_size 5 -> groups [0:5], [5:6] -> exactly one sleep.
        assert sleeps == [0.0]

    @pytest.mark.asyncio
    async def test_default_group_delay_is_sixty(self, pm, manager, tmp_path):
        """mutmut_2: default group_delay 60.0 -> 61.0."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        sleeps = []

        async def fake_sleep(t):
            sleeps.append(t)

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"),
        ):
            # 2 configs, group_size 1 -> one inter-group sleep at the default delay.
            await pm.spawn_swarm([str(cfg)] * 2, group_size=1)

        assert 60.0 in sleeps
        assert 61.0 not in sleeps

    @pytest.mark.asyncio
    async def test_default_name_style_is_random(self, pm, manager, tmp_path):
        """mutmut_3/4: default name_style 'random' -> 'XXrandomXX'/'RANDOM'."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"):
            await pm.spawn_swarm([str(cfg)])  # no name_style -> default

        assert pm._spawn_name_style == "random"

    @pytest.mark.asyncio
    async def test_default_name_base_is_empty(self, pm, manager, tmp_path):
        """mutmut_5: default name_base '' -> 'XXXX'."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"):
            await pm.spawn_swarm([str(cfg)])  # no name_base -> default

        assert pm._spawn_name_base == ""

    # --- pre-registration loop ---------------------------------------------
    @pytest.mark.asyncio
    async def test_preregister_index_is_addition(self, pm, manager, tmp_path):
        """mutmut_15: agent_id = f'agent_{base_index + i:03d}' -> base_index - i.
        With 3 configs from base_index 0, the addition produces agent_000..002;
        subtraction produces agent_000, agent_-01, agent_-02."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"):
            await pm.spawn_swarm([str(cfg)] * 3, group_size=1, group_delay=0.0)

        assert "agent_001" in manager.agents
        assert "agent_002" in manager.agents

    @pytest.mark.asyncio
    async def test_preregister_pid_is_zero(self, pm, manager, tmp_path):
        """mutmut_19/23/26: queued agent pid must be exactly 0 (not None/dropped/1)."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        # No-op spawn so the queued AgentStatus is not overwritten.
        with patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"):
            await pm.spawn_swarm([str(cfg)])

        assert manager.agents["agent_000"].pid == 0

    @pytest.mark.asyncio
    async def test_preregister_config_is_path(self, pm, manager, tmp_path):
        """mutmut_20/24: queued agent config must be the config path (not None/dropped)."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"):
            await pm.spawn_swarm([str(cfg)])

        assert manager.agents["agent_000"].config == str(cfg)

    # --- bid index in the group loop ---------------------------------------
    @pytest.mark.asyncio
    async def test_bid_index_is_addition(self, pm, manager, tmp_path):
        """mutmut_46: bid = f'agent_{base_index + group_start + i:03d}' ->
        base_index - group_start + i. group_size=1 so each group advances
        group_start; subtraction would yield agent_-01/-02 for later groups."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        bids = []

        async def spy(c, bid):
            bids.append(bid)
            return bid

        with patch.object(pm, "spawn_agent", side_effect=spy):
            await pm.spawn_swarm([str(cfg)] * 3, group_size=1, group_delay=0.0)

        assert bids == ["agent_000", "agent_001", "agent_002"]

    # --- logger.exception on spawn failure ---------------------------------
    @pytest.mark.asyncio
    async def test_spawn_failure_logged_with_args(self, pm, manager, tmp_path):
        """mutmut_52-62: logger.exception(event, agent_id=, config=, error=)."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        async def boom(c, bid):
            raise RuntimeError("boom")

        with (
            patch.object(pm, "spawn_agent", side_effect=boom),
            patch("provide.uterm.manager.process_impl.logger") as mlog,
        ):
            await pm.spawn_swarm([str(cfg)])

        # Locate the swarm-level exception call.
        exc_calls = [c for c in mlog.exception.call_args_list if c.args and c.args[0] == "agent_spawn_failed_in_group"]
        assert len(exc_calls) == 1  # kills mutmut_52/60/61 (event-name mutations)
        call = exc_calls[0]
        assert call.kwargs.get("agent_id") == "agent_000"  # mutmut_53/57
        assert call.kwargs.get("config") == str(cfg)  # mutmut_54/58
        assert call.kwargs.get("error") == "boom"  # mutmut_55/59/62

    # --- logger.info on completion -----------------------------------------
    @pytest.mark.asyncio
    async def test_completion_logged_with_args(self, pm, manager, tmp_path):
        """mutmut_65-72: logger.info('swarm_spawn_complete', started=, total=)."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with (
            patch.object(pm, "spawn_agent", new_callable=AsyncMock, return_value="x"),
            patch("provide.uterm.manager.process_impl.logger") as mlog,
        ):
            await pm.spawn_swarm([str(cfg), str(cfg)], group_size=2)

        info_calls = [c for c in mlog.info.call_args_list if c.args and c.args[0] == "swarm_spawn_complete"]
        assert len(info_calls) == 1  # kills mutmut_65/71/72 (event-name mutations)
        call = info_calls[0]
        # Two configs, two successful spawns -> started == 2, total == 2.
        assert call.kwargs.get("started") == 2  # mutmut_66/69
        assert call.kwargs.get("total") == 2  # mutmut_67/70


# ---- _spawn_platform_kwargs (24 killed, 0 equiv) ----
class TestSpawnPlatformKwargs:
    """Kill-tests for AgentProcessManager._spawn_platform_kwargs (mutants 1-25).

    The method is fully determined by os.name and _build_preexec_rlimit_fn().
    On the real test platform os.name == "posix" so the nt branch is dead;
    we patch provide.uterm.manager.process_impl.os.name to "nt" (and the
    subprocess.CREATE_NEW_PROCESS_GROUP attribute) to exercise it.
    """

    # --- POSIX branch (real os.name == "posix") -----------------------------

    def test_posix_returns_start_new_session_true(self, pm):
        """Original (POSIX) returns exactly {'start_new_session': True} when no
        preexec fn is built.

        Kills mutmut_1 (`os.name != 'nt'` inverts -> takes nt branch which on
        real POSIX lacks CREATE_NEW_PROCESS_GROUP -> {}), mutmut_18 (key
        'XXstart_new_sessionXX'), mutmut_19 (key 'START_NEW_SESSION'),
        mutmut_20 (value False).
        """
        with patch.object(pm, "_build_preexec_rlimit_fn", return_value=None):
            result = pm._spawn_platform_kwargs()
        assert result == {"start_new_session": True}

    def test_posix_with_preexec_includes_preexec_fn(self, pm):
        """When _build_preexec_rlimit_fn returns a fn, it is added under the
        exact key 'preexec_fn' with that exact value.

        Kills mutmut_21 (preexec=None drops it), mutmut_22 (inverted `is None`
        check drops it), mutmut_23 (value forced to None), mutmut_24 (key
        'XXpreexec_fnXX'), mutmut_25 (key 'PREEXEC_FN').
        """
        sentinel = object()
        with patch.object(pm, "_build_preexec_rlimit_fn", return_value=sentinel):
            result = pm._spawn_platform_kwargs()
        assert result == {"start_new_session": True, "preexec_fn": sentinel}

    def test_posix_preexec_builder_is_invoked(self, pm):
        """The preexec builder is actually called (mutmut_21 replaces the call
        with a literal None and never invokes it)."""
        called = {"n": 0}

        def _fake():
            called["n"] += 1
            return

        with patch.object(pm, "_build_preexec_rlimit_fn", side_effect=_fake):
            pm._spawn_platform_kwargs()
        assert called["n"] == 1

    # --- nt branch, CREATE_NEW_PROCESS_GROUP present ------------------------

    def test_nt_attr_present_returns_creationflags(self, pm):
        """With os.name 'nt' and CREATE_NEW_PROCESS_GROUP present (=512),
        original returns exactly {'creationflags': 512}.

        Kills mutmut_2 ('XXntXX') & mutmut_3 ('NT') (compare false -> else
        branch -> {'start_new_session': True}); mutmut_6
        (getattr(None,...)=0 -> {}); mutmut_12 ('XXCREATE_NEW_PROCESS_GROUPXX')
        & mutmut_13 ('create_new_process_group') (attr missing -> default 0 ->
        {}); mutmut_15 (key 'XXcreationflagsXX') & mutmut_16 (key
        'CREATIONFLAGS'). Also kills mutmut_4/5 (int(None) TypeError) and
        mutmut_7/9/10 (bad getattr name TypeError) since those raise instead
        of returning the dict.
        """
        from provide.uterm.manager import process_impl

        with (
            patch.object(process_impl.os, "name", "nt"),
            patch.object(process_impl.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
        ):
            result = pm._spawn_platform_kwargs()
        assert result == {"creationflags": 512}

    def test_nt_attr_present_does_not_raise(self, pm):
        """Original must not raise and must surface flags==512.

        Reinforces the kills of mutmut_4 (flags=None), mutmut_5 (int(None)),
        mutmut_7 (getattr(subprocess, None, 0)), mutmut_9
        (getattr('CREATE_NEW_PROCESS_GROUP', 0)), mutmut_10
        (getattr(subprocess, 0)) — each raises TypeError on this path.
        """
        from provide.uterm.manager import process_impl

        with (
            patch.object(process_impl.os, "name", "nt"),
            patch.object(process_impl.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
        ):
            result = pm._spawn_platform_kwargs()
        assert result.get("creationflags") == 512

    # --- nt branch, CREATE_NEW_PROCESS_GROUP absent -------------------------

    def test_nt_attr_absent_returns_empty(self, pm):
        """With os.name 'nt' but CREATE_NEW_PROCESS_GROUP absent, getattr's
        default 0 makes flags falsy -> the ternary returns {}.

        Kills mutmut_8 (default None -> int(None) TypeError), mutmut_11
        (no default -> AttributeError), mutmut_14 (default 1 -> truthy ->
        {'creationflags': 1}). Original returns {}.
        """
        from provide.uterm.manager import process_impl

        sub = process_impl.subprocess
        had = hasattr(sub, "CREATE_NEW_PROCESS_GROUP")
        saved = getattr(sub, "CREATE_NEW_PROCESS_GROUP", None)
        if had:
            delattr(sub, "CREATE_NEW_PROCESS_GROUP")
        try:
            with patch.object(process_impl.os, "name", "nt"):
                result = pm._spawn_platform_kwargs()
        finally:
            if had:
                sub.CREATE_NEW_PROCESS_GROUP = saved
        assert result == {}


# ---- _load_worker_type (19 killed, 2 equiv) ----
class TestLoadWorkerTypeKills:
    async def test_valid_file_returns_worker_type_and_raw(self, pm, tmp_path):
        """Kills 2,3,4,5,6,7,11,13,15,16: a real lowercase 'worker_type'
        key must be read through into both return values.

        - 2 (raw_text=None), 7 (safe_load(None)): parse yields {}/default.
        - 3 (to_thread(None)), 4 (Path(None)): except path -> default,{}.
        - 5 (raw=None): raw.get raises -> except -> ('default', None).
        - 6 (safe_load and {}): truthy-and-{} -> {} -> default.
        - 11 (get(None,...)), 13 (get('default')), 15/16 (wrong key str):
          worker_type lookup misses -> 'default'.
        """
        cfg = tmp_path / "agent.yaml"
        cfg.write_text("worker_type: test_game\nfoo: bar\n")
        worker_type, raw = await pm._load_worker_type(str(cfg))
        assert worker_type == "test_game"
        assert raw == {"worker_type": "test_game", "foo": "bar"}

    async def test_missing_worker_type_key_defaults(self, pm, tmp_path):
        """Reinforces read-through: when the key is absent the dict is still
        parsed and worker_type falls back to 'default'."""
        cfg = tmp_path / "nokey.yaml"
        cfg.write_text("foo: bar\n")
        worker_type, raw = await pm._load_worker_type(str(cfg))
        assert worker_type == "default"
        assert raw == {"foo": "bar"}

    async def test_read_failure_returns_empty_dict(self, pm, tmp_path):
        """Kills mutmut_1: on read failure the initial raw value ({}) is
        returned, NOT None (the except path never reassigns raw)."""
        missing = tmp_path / "does_not_exist.yaml"
        worker_type, raw = await pm._load_worker_type(str(missing))
        assert worker_type == "default"
        assert raw == {}
        assert raw is not None

    async def test_read_failure_logs_warning(self, pm, tmp_path):
        """Kills 21,22,23,25,26,27,28,29: the warning event name and kwargs
        emitted on the exception path."""
        missing = tmp_path / "nope.yaml"
        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            await pm._load_worker_type(str(missing))
        mock_logger.warning.assert_called_once()
        args, kwargs = mock_logger.warning.call_args
        # message positional (kills 21=None, 27=XX-wrapped, 28=uppercased)
        assert args[0] == "worker_type_read_failed"
        # config_path kwarg present and correct (kills 22=None, 25=dropped)
        assert "config_path" in kwargs
        assert kwargs["config_path"] == str(missing)
        # error kwarg present + a real non-empty error string
        # (kills 23=None, 26=dropped, 29=str(None)=='None')
        assert "error" in kwargs
        assert kwargs["error"] is not None
        assert isinstance(kwargs["error"], str)
        assert kwargs["error"] != "None"
        assert kwargs["error"] != ""


# ---- kill_agent (18 killed, 3 equiv) ----
class TestKillAgentExtra:
    @pytest.mark.asyncio
    async def test_fallback_pid_from_agent_passed_to_stop_tree(self, pm, manager):
        """Kills 7,8,9,11,12,13,15,16,18,19,22: with no live process and an
        agent carrying pid=99, kill_agent must compute fallback_pid=99 and pass
        pid=99 to _stop_process_tree. Mutants that null the agent lookup
        (agent=None / agents.get(None) / getattr(None,...)), force the value to
        0/None ('... and 0', fallback_pid=None), swap the process-None branch
        ('process is not None'), read a wrong attribute name ('XXpidXX'/'PID'),
        or crash (getattr with a non-str name -> TypeError) all change the
        observed pid or abort before the call."""
        manager.agents["agent_x"] = AgentStatusBase(agent_id="agent_x", pid=99, state="running")
        # deliberately NOT registered in manager.processes -> process is None branch
        manager.broadcast_status = AsyncMock()
        captured = {}

        async def spy(**kw):
            captured.update(kw)

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_x")

        assert captured["pid"] == 99
        assert captured["agent_id"] == "agent_x"

    @pytest.mark.asyncio
    async def test_int_none_does_not_crash_pid_is_99(self, pm, manager):
        """Kills 10: int(None) raises TypeError; original computes pid=99 and
        completes cleanly. Also reinforces the TypeError-raising mutants 13/15/16."""
        manager.agents["agent_y"] = AgentStatusBase(agent_id="agent_y", pid=99, state="running")
        manager.broadcast_status = AsyncMock()
        captured = {}

        async def spy(**kw):
            captured.update(kw)

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_y")  # must not raise

        assert captured.get("pid") == 99

    @pytest.mark.asyncio
    async def test_zero_pid_falls_back_to_none_not_one(self, pm, manager):
        """Kills 21: 'or 0' -> 'or 1'. With agent.pid=0 and no process, the
        original yields fallback_pid=0 -> pid=(0 or None)=None; the mutant yields
        1 -> pid=1."""
        manager.agents["agent_z"] = AgentStatusBase(agent_id="agent_z", pid=0, state="running")
        manager.broadcast_status = AsyncMock()
        captured = {}

        async def spy(**kw):
            captured.update(kw)

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_z")

        assert captured["pid"] is None

    @pytest.mark.asyncio
    async def test_agent_id_forwarded_to_stop_tree(self, pm, manager):
        """Kills 24: agent_id=None passed to _stop_process_tree."""
        manager.agents["agent_a"] = AgentStatusBase(agent_id="agent_a", pid=5, state="running")
        manager.broadcast_status = AsyncMock()
        captured = {}

        async def spy(**kw):
            captured.update(kw)

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_a")

        assert captured["agent_id"] == "agent_a"
        assert captured["agent_id"] is not None

    @pytest.mark.asyncio
    async def test_pid_kwarg_present_and_correct(self, pm, manager):
        """Kills 26 (pid=None literal) and 30 (pid= kwarg dropped): both change
        the pid the stop-tree call sees away from the agent's real pid (77)."""
        manager.agents["agent_b"] = AgentStatusBase(agent_id="agent_b", pid=77, state="running")
        manager.broadcast_status = AsyncMock()
        captured = {}

        async def spy(**kw):
            captured.update(kw)

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_b")

        assert "pid" in captured  # kills mutmut_30 (kwarg dropped -> key absent)
        assert captured["pid"] == 77  # kills mutmut_26 (pid=None literal)

    @pytest.mark.asyncio
    async def test_timeout_kwarg_forwarded(self, pm, manager):
        """Kills 31: timeout_s= kwarg dropped from the _stop_process_tree call."""
        manager.agents["agent_c"] = AgentStatusBase(agent_id="agent_c", pid=3, state="running")
        manager.broadcast_status = AsyncMock()
        captured = {}

        async def spy(**kw):
            captured.update(kw)

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_c")

        assert "timeout_s" in captured
        assert captured["timeout_s"] == 5.0

    @pytest.mark.asyncio
    async def test_pop_missing_process_does_not_raise(self, pm, manager):
        """Kills 40: processes.pop(agent_id) without the None default raises
        KeyError when agent_id is absent from manager.processes. The original
        tolerates the absence and proceeds to mark the agent stopped."""
        manager.agents["agent_d"] = AgentStatusBase(agent_id="agent_d", pid=11, state="running")
        # deliberately NOT present in manager.processes
        assert "agent_d" not in manager.processes
        manager.broadcast_status = AsyncMock()

        async def spy(**kw):
            return None

        with patch.object(pm, "_stop_process_tree", side_effect=spy):
            await pm.kill_agent("agent_d")  # mutant raises KeyError on the pop

        assert manager.agents["agent_d"].state == "stopped"


# ---- release_agent_account (14 killed, 0 equiv) ----
class TestReleaseAgentAccountKills:
    """Kill release_agent_account survivors (mutants 9-23, sans 18).

    All survivors live in the try/except body: the success-path
    ``logger.info("manager_released_account", agent_id=agent_id)`` and the
    failure-path ``logger.warning("account_release_failed", agent_id=agent_id,
    error=str(e))``. We assert on the exact (event, **kwargs) of those calls,
    with release_by_agent returning truthy (info path) or raising (warning path).
    """

    # ---- success path: logger.info ----  (mutants 9, 10, 11, 12, 13, 14)
    def test_success_logs_info_with_exact_args(self, pm, manager):
        pool = MagicMock()
        pool.release_by_agent.return_value = True
        manager.account_pool = pool

        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            pm.release_agent_account("agent-007")

        # release_by_agent must be invoked with cooldown_s=0
        pool.release_by_agent.assert_called_once_with(agent_id="agent-007", cooldown_s=0)
        # info logged on the truthy release with the exact event name + agent_id
        mock_logger.info.assert_called_once_with("manager_released_account", agent_id="agent-007")
        mock_logger.warning.assert_not_called()

    def test_success_info_event_name_is_literal(self, pm, manager):
        # Defense-in-depth for the string-mutants (13/14) and None (9): assert
        # the event arg is exactly the lower-case literal, and agent_id kwarg.
        pool = MagicMock()
        pool.release_by_agent.return_value = True
        manager.account_pool = pool

        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            pm.release_agent_account("agent-007")

        args, kwargs = mock_logger.info.call_args
        assert args == ("manager_released_account",)  # kills 9 (None), 11 (dropped positional), 13, 14
        assert kwargs == {"agent_id": "agent-007"}  # kills 10 (None), 12 (dropped kwarg)

    # ---- failure path: logger.warning ----  (mutants 15-17, 19-23)
    def test_failure_logs_warning_with_exact_args(self, pm, manager):
        pool = MagicMock()
        pool.release_by_agent.side_effect = RuntimeError("boom-msg")
        manager.account_pool = pool

        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            # exception must be swallowed: no raise
            pm.release_agent_account("agent-xyz")

        mock_logger.warning.assert_called_once_with("account_release_failed", agent_id="agent-xyz", error="boom-msg")
        mock_logger.info.assert_not_called()

    def test_failure_warning_event_name_is_literal(self, pm, manager):
        pool = MagicMock()
        pool.release_by_agent.side_effect = RuntimeError("boom-msg")
        manager.account_pool = pool

        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            pm.release_agent_account("agent-xyz")

        args, kwargs = mock_logger.warning.call_args
        # kills 15 (None), 21 (XX-wrapped), 22 (uppercase)
        assert args == ("account_release_failed",)
        # kills 16 (agent_id=None), 17/23 (error not real message), 19/20 (dropped kwargs)
        assert kwargs == {"agent_id": "agent-xyz", "error": "boom-msg"}

    def test_failure_error_is_str_of_exception_not_literal_none(self, pm, manager):
        # Specifically targets mutant 23: error=str(None) would be "None".
        pool = MagicMock()
        pool.release_by_agent.side_effect = ValueError("the-real-error")
        manager.account_pool = pool

        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            pm.release_agent_account("a1")

        _, kwargs = mock_logger.warning.call_args
        assert kwargs["error"] == "the-real-error"
        assert kwargs["error"] != "None"


# ---- _launch_queued_agent (8 killed, 0 equiv) ----
class TestLaunchQueuedAgentLogging:
    """_launch_queued_agent — kills mutmut_5/6/7/9/10/11/12/13, all of which
    mutate the logger.exception(...) call emitted when spawn_agent raises."""

    async def test_exception_log_exact_call_args(self, pm, manager):
        """Kills mutmut_5/6/7/9/10/11/12/13: the logger.exception call must use
        the exact event name 'stale_queued_agent_launch_failed' plus
        agent_id=<id> and error=<str(exception)>."""
        pm.spawn_agent = AsyncMock(side_effect=RuntimeError("boom"))
        manager.broadcast_status = AsyncMock()
        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            await pm._launch_queued_agent("agent_042", "cfg.yaml")
        mock_logger.exception.assert_called_once_with(
            "stale_queued_agent_launch_failed",
            agent_id="agent_042",
            error="boom",
        )

    async def test_exception_log_event_name_literal(self, pm, manager):
        """mutmut_5/11/12: positional event name must be the exact lowercase
        literal, not None, not 'XXstale_queued_agent_launch_failedXX', not
        uppercased."""
        pm.spawn_agent = AsyncMock(side_effect=RuntimeError("x"))
        manager.broadcast_status = AsyncMock()
        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            await pm._launch_queued_agent("a", "c")
        assert len(mock_logger.exception.call_args.args) == 1
        assert mock_logger.exception.call_args.args[0] == "stale_queued_agent_launch_failed"

    async def test_exception_log_error_is_message_not_literal_none(self, pm, manager):
        """mutmut_7/13: error kwarg must be str(e) (the real message), not None
        and not the string 'None' (str(None))."""
        pm.spawn_agent = AsyncMock(side_effect=ValueError("real_failure_detail"))
        manager.broadcast_status = AsyncMock()
        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            await pm._launch_queued_agent("a", "c")
        kw = mock_logger.exception.call_args.kwargs
        assert "error" in kw  # mutmut_10 drops error entirely
        assert kw["error"] == "real_failure_detail"
        assert kw["error"] is not None
        assert kw["error"] != "None"

    async def test_exception_log_agent_id_kwarg_present(self, pm, manager):
        """mutmut_6/9: agent_id kwarg must be passed and equal to the real id."""
        pm.spawn_agent = AsyncMock(side_effect=RuntimeError("boom"))
        manager.broadcast_status = AsyncMock()
        with patch("provide.uterm.manager.process_impl.logger") as mock_logger:
            await pm._launch_queued_agent("agent_777", "c")
        kw = mock_logger.exception.call_args.kwargs
        assert "agent_id" in kw  # mutmut_9 drops agent_id entirely
        assert kw["agent_id"] == "agent_777"


# ---- monitor_processes (7 killed, 1 equiv) ----
class TestMonitorProcessesMonitorLoopKills:
    """Kill-tests for ``AgentProcessManager.monitor_processes``.

    The method is an infinite ``while True`` loop. Each test patches the loop's
    helper functions (``_handle_*`` and ``_cleanup_old_worker_logs``) on the
    ``process_impl`` module and replaces ``asyncio.sleep`` (the final statement
    of every loop body) with a counter that raises a private ``_StopLoopError``
    sentinel after exactly N completed iterations — so we can observe the
    behaviour of iteration N (including the ~hourly log-cleanup trigger).
    """

    _PI = "provide.uterm.manager.process_impl"

    class _StopLoopError(Exception):
        """Sentinel used to break the infinite monitor loop after N iterations."""

    async def _run_n_iterations(self, pm, n):
        """Run ``monitor_processes`` for exactly ``n`` complete loop iterations.

        Returns the patched ``_cleanup_old_worker_logs`` mock so callers can
        assert on whether (and with what arg) the hourly cleanup fired.
        """
        cleanup = MagicMock()
        counter = {"i": 0}
        stop = self._StopLoopError

        async def fake_sleep(_interval):
            counter["i"] += 1
            if counter["i"] >= n:
                raise stop

        with (
            patch(f"{self._PI}._handle_exited_processes", new=AsyncMock()),
            patch(f"{self._PI}._handle_heartbeat_timeouts", new=AsyncMock()),
            patch(f"{self._PI}._handle_stale_queued", new=MagicMock()),
            patch(f"{self._PI}._handle_bust_respawn", new=AsyncMock()),
            patch(f"{self._PI}._handle_desired_state", new=AsyncMock()),
            patch(f"{self._PI}._cleanup_old_worker_logs", new=cleanup),
            patch(f"{self._PI}.asyncio.sleep", new=fake_sleep),
        ):
            with contextlib.suppress(stop):
                await pm.monitor_processes()
        return cleanup

    # -- mutmut_16 (`% 360 != 0`) and mutmut_17 (`% 360 == 1`): the hourly
    #    cleanup must NOT fire on the very first iteration. Both mutants would
    #    call it on iteration 1. --
    async def test_no_cleanup_on_first_iteration(self, pm):
        cleanup = await self._run_n_iterations(pm, 1)
        cleanup.assert_not_called()

    # -- mutmut_2 (`_monitor_iter = 1`): starting the counter at 1 makes the
    #    cleanup fire one iteration early (iteration 359). The original starts
    #    at 0, so nothing fires before iteration 360. --
    async def test_no_cleanup_before_360_iterations(self, pm):
        cleanup = await self._run_n_iterations(pm, 359)
        cleanup.assert_not_called()

    # -- mutmut_13 (`_monitor_iter += 2`): doubling the step makes the trigger
    #    value 360 land on iteration 180 instead of 360. --
    async def test_no_cleanup_at_180_iterations(self, pm):
        cleanup = await self._run_n_iterations(pm, 180)
        cleanup.assert_not_called()

    # -- Positive baseline + mutmut_19 (`_cleanup_old_worker_logs(None)`):
    #    cleanup fires exactly at iteration 360 and is passed ``self`` (the
    #    AgentProcessManager), never ``None``. This also reinforces the kills
    #    for mutmut_2/13/16/17 (which all shift the trigger off iteration 360). --
    async def test_cleanup_fires_once_at_360_with_self(self, pm):
        cleanup = await self._run_n_iterations(pm, 360)
        cleanup.assert_called_once_with(pm)
        (arg,), _ = cleanup.call_args
        assert arg is pm
        assert arg is not None

    # -- mutmut_18 (`contextlib.suppress(None)`): a failing cleanup must be
    #    swallowed so the monitor loop survives. The original wraps the call in
    #    ``suppress(Exception)``; the mutant's ``suppress(None)`` suppresses
    #    nothing (and itself raises ``TypeError`` from ``issubclass(..., None)``)
    #    so the error escapes the loop. We make cleanup raise on its only call
    #    (iteration 360) and require that the loop nonetheless reaches the next
    #    ``sleep`` and exits via our ``_StopLoopError`` sentinel. --
    async def test_cleanup_exception_is_suppressed(self, pm):
        boom = RuntimeError("cleanup boom")
        counter = {"i": 0}
        stop = self._StopLoopError

        def raiser(_self):
            raise boom

        cleanup = MagicMock(side_effect=raiser)

        async def fake_sleep(_interval):
            # cleanup runs on the 360th loop while counter is still 359; the
            # 360th sleep then bumps counter to 360 and stops the loop.
            counter["i"] += 1
            if counter["i"] >= 360:
                raise stop

        with (
            patch(f"{self._PI}._handle_exited_processes", new=AsyncMock()),
            patch(f"{self._PI}._handle_heartbeat_timeouts", new=AsyncMock()),
            patch(f"{self._PI}._handle_stale_queued", new=MagicMock()),
            patch(f"{self._PI}._handle_bust_respawn", new=AsyncMock()),
            patch(f"{self._PI}._handle_desired_state", new=AsyncMock()),
            patch(f"{self._PI}._cleanup_old_worker_logs", new=cleanup),
            patch(f"{self._PI}.asyncio.sleep", new=fake_sleep),
        ):
            # Original: RuntimeError is suppressed, the loop continues and the
            # next sleep raises _StopLoopError. Mutant: the error escapes instead, so
            # _StopLoopError is never reached.
            with pytest.raises(stop):
                await pm.monitor_processes()
        cleanup.assert_called_once()

    # -- mutmut_6 (`if t.done()`): the spawn-task list must be pruned to the
    #    tasks that are NOT done. The mutant inverts the filter and keeps the
    #    finished task while dropping the live one. --
    async def test_spawn_tasks_keeps_live_drops_done(self, pm):
        done_task = MagicMock()
        done_task.done.return_value = True
        live_task = MagicMock()
        live_task.done.return_value = False
        pm._spawn_tasks = [done_task, live_task]

        await self._run_n_iterations(pm, 1)

        assert pm._spawn_tasks == [live_task]
        assert done_task not in pm._spawn_tasks


# ---- _build_worker_env (7 killed, 0 equiv) ----
class TestBuildWorkerEnv:
    def test_passthrough_membership_direction(self, pm):
        # mutmut_4: ``k in _WORKER_ENV_PASSTHROUGH`` -> ``k not in``.
        # A non-prefixed, non-passthrough var must NOT leak into the worker env;
        # the mutant (which keeps everything *not* in the passthrough set) leaks it
        # and conversely drops the passthrough PATH var.
        fake_env = {
            "PATH": "/usr/bin",  # passthrough -> kept (original) / dropped (mutant)
            "MY_SECRET_LEAK": "boom",  # neither -> dropped (original) / kept (mutant)  # pragma: allowlist secret
        }
        with patched_environ(fake_env):
            env = pm._build_worker_env("UTERM_", None, MagicMock(), {}, "agent_000")
        assert "MY_SECRET_LEAK" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_name_style_value(self, pm):
        # mutmut_9: NAME_STYLE assigned ``None`` instead of the configured style.
        pm._spawn_name_style = "random"
        with patched_environ({}):
            env = pm._build_worker_env("UTERM_", None, MagicMock(), {}, "agent_000")
        assert env["UTERM_NAME_STYLE"] == "random"

    def test_configure_called_when_agent_entry_present(self, pm):
        # mutmut_11: ``is not None`` -> ``is None``. With a real (non-None)
        # agent_entry the original invokes configure_worker_env; mutant skips it.
        reg = MagicMock()
        sentinel = object()
        with patched_environ({}):
            pm._build_worker_env("UTERM_", sentinel, reg, {"k": "v"}, "agent_000")
        reg.configure_worker_env.assert_called_once()

    def test_configure_agent_entry_arg(self, pm):
        # mutmut_13: 2nd positional arg ``agent_entry`` -> ``None``.
        reg = MagicMock()
        sentinel = object()
        with patched_environ({}):
            pm._build_worker_env("UTERM_", sentinel, reg, {"k": "v"}, "agent_000")
        args, _kwargs = reg.configure_worker_env.call_args
        assert args[1] is sentinel

    def test_configure_manager_arg(self, pm, manager):
        # mutmut_14: 3rd positional arg ``self.manager`` -> ``None``.
        reg = MagicMock()
        sentinel = object()
        with patched_environ({}):
            pm._build_worker_env("UTERM_", sentinel, reg, {"k": "v"}, "agent_000")
        args, _kwargs = reg.configure_worker_env.call_args
        assert args[2] is manager

    def test_configure_raw_config_value(self, pm):
        # mutmut_15: ``raw_config=raw_config`` -> ``raw_config=None``.
        reg = MagicMock()
        sentinel = object()
        raw = {"some": "config"}
        with patched_environ({}):
            pm._build_worker_env("UTERM_", sentinel, reg, raw, "agent_000")
        _args, kwargs = reg.configure_worker_env.call_args
        assert kwargs.get("raw_config") == raw
        assert kwargs.get("raw_config") is not None

    def test_configure_raw_config_kwarg_present(self, pm):
        # mutmut_19: drop the ``raw_config=`` kwarg entirely.
        reg = MagicMock()
        sentinel = object()
        raw = {"some": "config"}
        with patched_environ({}):
            pm._build_worker_env("UTERM_", sentinel, reg, raw, "agent_000")
        _args, kwargs = reg.configure_worker_env.call_args
        assert "raw_config" in kwargs


# ---- _scope_worker_tokens (3 killed, 4 equiv) ----
class TestScopeWorkerTokens:
    """Kill survivors for AgentProcessManager._scope_worker_tokens.

    The single non-doc line targeted by these mutants is::

        worker_var = getattr(config, "auth_worker_token_env_var", "UTERM_MANAGER_WORKER_TOKEN")

    Mutants 4/10/11 make ``getattr`` miss the real config attribute (bad object
    ``None``, mangled ``XX..XX`` attr name, upper-cased attr name) so it falls
    back to the literal default ``"UTERM_MANAGER_WORKER_TOKEN"`` instead of
    reading the configured field. By pointing
    ``config.auth_worker_token_env_var`` at a *custom* var name and only
    populating that custom var (with the literal default var explicitly unset),
    the original reads the secret (derives + injects + strips the custom var)
    while the mutants read the empty default and do nothing observable.
    """

    def test_custom_worker_var_is_read_for_derivation(self, pm, manager):
        from provide.uterm.manager.auth import derive_agent_token

        # config drives which env var holds the fleet worker secret.
        manager.config.auth_worker_token_env_var = "CUSTOM_WORKER_TOKEN_VAR"
        operator_var = manager.config.auth_token_env_var
        secret = "fleet-secret-xyz"  # pragma: allowlist secret
        agent_id = "agent-7"
        env: dict[str, str] = {}
        with patch.dict(os.environ, {"CUSTOM_WORKER_TOKEN_VAR": secret}, clear=False):
            # ensure the literal default var is absent so a mutant cannot
            # accidentally read a real value from it.
            os.environ.pop("UTERM_MANAGER_WORKER_TOKEN", None)
            pm._scope_worker_tokens(env, agent_id)
        # Original reads CUSTOM_WORKER_TOKEN_VAR -> derives & injects operator token.
        # Mutants 4/10/11 read default UTERM_MANAGER_WORKER_TOKEN (unset) -> no injection.
        assert env[operator_var] == derive_agent_token(secret, agent_id)

    def test_custom_worker_var_is_stripped(self, pm, manager):
        manager.config.auth_worker_token_env_var = "CUSTOM_WORKER_TOKEN_VAR"
        env = {"CUSTOM_WORKER_TOKEN_VAR": "leaked-secret"}
        with patch.dict(os.environ, {"CUSTOM_WORKER_TOKEN_VAR": "fleet"}, clear=False):
            os.environ.pop("UTERM_MANAGER_WORKER_TOKEN", None)
            pm._scope_worker_tokens(env, "agent-x")
        # Original pops the custom var (it knows the configured name).
        # Mutants 4/10/11 would pop the default name instead, leaving the custom var.
        assert "CUSTOM_WORKER_TOKEN_VAR" not in env

    def test_no_worker_token_leaves_operator_untouched(self, pm, manager):
        # Sanity guard for the no-token path: when the configured var is unset,
        # no operator token is injected.
        manager.config.auth_worker_token_env_var = "CUSTOM_WORKER_TOKEN_VAR_ABSENT"
        operator_var = manager.config.auth_token_env_var
        env: dict[str, str] = {}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CUSTOM_WORKER_TOKEN_VAR_ABSENT", None)
            pm._scope_worker_tokens(env, "agent-q")
        assert operator_var not in env


# ---- note_agent_id (6 killed, 0 equiv) ----
class TestNoteAgentIdKills:
    def test_valid_id_advances_index_to_idx_plus_one(self, pm):
        """Original: note_agent_id('agent_005') -> _next_agent_index = max(0, 5+1) = 6.

        Kills:
          mutmut_1 (idx=None -> early return, stays 0),
          mutmut_2 (_parse_agent_index(None) -> None -> early return, stays 0),
          mutmut_3 (if idx is not None: return -> valid id returns early, stays 0),
          mutmut_4 (_next_agent_index = None -> not 6),
          mutmut_9 (idx - 1 -> 4, not 6),
          mutmut_10 (idx + 2 -> 7, not 6).
        """
        pm._next_agent_index = 0
        pm.note_agent_id("agent_005")
        assert pm._next_agent_index == 6

    def test_result_is_int_not_none(self, pm):
        """mutmut_4: _next_agent_index = None. Original keeps an int."""
        pm._next_agent_index = 0
        pm.note_agent_id("agent_007")
        assert isinstance(pm._next_agent_index, int)
        assert pm._next_agent_index == 8

    def test_invalid_id_does_not_advance_or_crash(self, pm):
        """Original early-returns for unparseable id (idx is None).

        Guards against mutmut_3 turning the None branch into the update path
        (which would do None + 1 and raise / never early-return on bad input).
        Also confirms a non-matching id leaves the index untouched.
        """
        pm._next_agent_index = 3
        pm.note_agent_id("not-an-agent")
        assert pm._next_agent_index == 3

    def test_max_keeps_higher_existing_index(self, pm):
        """max(current, idx+1): a lower parsed id must not lower the index.

        Reinforces the +1/max arithmetic (mutmut_9/_10 offsets) against an
        already-advanced counter: agent_002 -> idx+1=3, but current 50 wins.
        """
        pm._next_agent_index = 50
        pm.note_agent_id("agent_002")
        assert pm._next_agent_index == 50


# ---- start_spawn_swarm (6 killed, 0 equiv) ----
class TestStartSpawnSwarmKills:
    async def _drain(self, pm):
        await asyncio.gather(*[t for t in pm._spawn_tasks if not t.done()], return_exceptions=True)

    @pytest.mark.asyncio
    async def test_group_size_default_is_1(self, pm):
        """mutmut_1: group_size default 1 -> 2. Call with no group_size so the
        default flows into spawn_swarm; assert it is 1."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await self._drain(pm)

        assert captured.get("group_size") == 1

    @pytest.mark.asyncio
    async def test_group_delay_default_is_12(self, pm):
        """mutmut_2: group_delay default 12.0 -> 13.0."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await self._drain(pm)

        assert captured.get("group_delay") == 12.0

    @pytest.mark.asyncio
    async def test_cancel_existing_default_true_invokes_cancel(self, pm):
        """mutmut_3: cancel_existing default True -> False. With the default,
        cancel_spawn() must be awaited exactly once; the mutant skips it."""
        cancel_calls = []

        async def fake_cancel():
            cancel_calls.append(True)
            return False

        async def noop(*a, **kw):
            return []

        with (
            patch.object(pm, "cancel_spawn", side_effect=fake_cancel),
            patch.object(pm, "spawn_swarm", side_effect=noop),
        ):
            await pm.start_spawn_swarm(["/a.yaml"])
            await self._drain(pm)

        assert cancel_calls == [True]

    @pytest.mark.asyncio
    async def test_name_style_default_is_lowercase_random(self, pm):
        """mutmut_4/5: name_style default 'random' -> 'XXrandomXX' / 'RANDOM'."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await self._drain(pm)

        assert captured.get("name_style") == "random"

    @pytest.mark.asyncio
    async def test_name_base_default_is_empty(self, pm):
        """mutmut_6: name_base default '' -> 'XXXX'."""
        captured = {}

        async def spy(paths, **kw):
            captured.update(kw)
            return []

        with patch.object(pm, "spawn_swarm", side_effect=spy):
            await pm.start_spawn_swarm(["/a.yaml"])
            await self._drain(pm)

        assert captured.get("name_base") == ""


# ---- _spawn_process (6 killed, 0 equiv) ----
class TestSpawnProcessKills:
    def _run_spawn(self, pm, cmd=None, env=None):
        """Call _spawn_process with Popen patched; return (popen_mock, log_path)."""
        if cmd is None:
            cmd = ["python", "-m", "worker"]
        if env is None:
            env = {"FOO": "bar"}
        import provide.uterm.manager.process_impl as process_impl

        popen = MagicMock(return_value=make_mock_proc())
        with patch.object(process_impl.subprocess, "Popen", popen):
            proc = pm._spawn_process("agent_000", cmd, env)
        return popen, proc

    def test_default_log_dir_is_logs_workers(self, manager, tmp_path):
        """mutmut_5: Path('logs/workers') -> Path('LOGS/WORKERS').

        Only reached when self._log_dir is falsy. Capture the log_dir via
        Path.mkdir so no real dirs are written and the case is OS-agnostic.
        """
        from pathlib import Path

        import provide.uterm.manager.process_impl as process_impl

        pm = AgentProcessManager(
            manager,
            worker_registry={"test_game": FakeWorkerPlugin()},
        )
        pm._log_dir = ""  # force the else branch
        captured: dict = {}

        def fake_mkdir(self, *a, **k):
            captured["dir"] = self

        def fake_open(self, *a, **k):
            return MagicMock()

        popen = MagicMock(return_value=make_mock_proc())
        with (
            patch.object(Path, "mkdir", fake_mkdir),
            patch.object(Path, "is_file", lambda self: False),
            patch.object(Path, "open", fake_open),
            patch.object(process_impl.subprocess, "Popen", popen),
        ):
            pm._spawn_process("agent_000", ["c"], {})

        assert captured["dir"] == Path("logs/workers")
        assert captured["dir"] != Path("LOGS/WORKERS")

    def test_suppress_targets_oserror(self, pm, tmp_path):
        """mutmut_14: contextlib.suppress(OSError) -> contextlib.suppress(None).

        The suppressed block wraps stat()/rename() during log rotation. Make
        the rotation path raise an OSError: with suppress(OSError) the call
        proceeds and returns a proc; with suppress(None) the exception escapes
        (TypeError raised by suppress.__exit__ on the non-exception None).
        """
        from pathlib import Path

        import provide.uterm.manager.process_impl as process_impl

        # Pre-create an oversized log so the rotation branch is entered.
        log_dir = Path(pm._log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "agent_000.log"
        log_file.write_text("x")

        popen = MagicMock(return_value=make_mock_proc())

        # Force the rotation-branch stat() to raise OSError. is_file() is
        # patched True so we don't depend on stat() for the branch guard.
        orig_stat = Path.stat

        def boom_stat(self, *a, **k):
            if self == log_file:
                raise OSError("disk gone")
            return orig_stat(self, *a, **k)

        with (
            patch.object(Path, "is_file", lambda self: self == log_file),
            patch.object(Path, "stat", boom_stat),
            patch.object(process_impl.subprocess, "Popen", popen),
        ):
            proc = pm._spawn_process("agent_000", ["c"], {})

        # Original: OSError suppressed -> spawn proceeds, Popen called.
        assert popen.called
        assert proc is popen.return_value

    def test_rotation_uses_strict_greater_than(self, pm):
        """mutmut_15: size > MAX -> size >= MAX.

        At size == MAX exactly: original does NOT rotate (no .prev created),
        mutant rotates (renames log to .prev).
        """
        from pathlib import Path

        import provide.uterm.manager.process_impl as process_impl
        from provide.uterm.manager.constants import WORKER_LOG_MAX_BYTES

        log_dir = Path(pm._log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "agent_000.log"
        log_file.write_text("seed")
        prev_file = log_dir / "agent_000.log.prev"

        popen = MagicMock(return_value=make_mock_proc())

        orig_stat = Path.stat

        class _FakeStat:
            st_size = WORKER_LOG_MAX_BYTES  # exactly equal to the boundary

        def fake_stat(self, *a, **k):
            if self == log_file:
                return _FakeStat()
            return orig_stat(self, *a, **k)

        with (
            patch.object(Path, "is_file", lambda self: self == log_file),
            patch.object(Path, "stat", fake_stat),
            patch.object(process_impl.subprocess, "Popen", popen),
        ):
            pm._spawn_process("agent_000", ["c"], {})

        # Original (strict >): no rotation at boundary -> no .prev.
        assert not prev_file.exists()

    def test_popen_first_positional_is_cmd_not_none(self, pm):
        """mutmut_26: subprocess.Popen(cmd, ...) -> Popen(None, ...)."""
        cmd = ["python", "-m", "worker", "--id", "agent_000"]
        popen, _ = self._run_spawn(pm, cmd=cmd)
        args = popen.call_args.args
        assert args[0] == cmd
        assert args[0] is not None

    def test_popen_called_with_cmd_positional(self, pm):
        """mutmut_30: cmd positional dropped entirely from Popen(...)."""
        cmd = ["python", "-m", "worker"]
        popen, _ = self._run_spawn(pm, cmd=cmd)
        args = popen.call_args.args
        assert len(args) >= 1
        assert args[0] == cmd

    def test_popen_includes_platform_kwargs(self, pm):
        """mutmut_34: **self._spawn_platform_kwargs() dropped from Popen(...)."""
        cmd = ["python", "-m", "worker"]
        plat = pm._spawn_platform_kwargs()
        assert plat, "platform kwargs expected non-empty on this OS"
        popen, _ = self._run_spawn(pm, cmd=cmd)
        kwargs = popen.call_args.kwargs
        for key in plat:
            assert key in kwargs


# ---- allocate_agent_id (4 killed, 0 equiv) ----
class TestAllocateAgentIdKills:
    def test_and_not_or_skips_collision(self, pm, manager):
        """mutmut_4: 'and' -> 'or' in the not-in-agents/not-in-processes condition.

        Force start idx=0 via a patched sync_next_agent_index, with agent_000
        present in manager.agents only (NOT in manager.processes). With the
        original 'and', agent_000 is a collision so the loop skips it and
        returns agent_001. With 'or', the condition short-circuits True on the
        first candidate and returns the colliding agent_000.
        """
        from provide.uterm.manager.models import AgentStatusBase

        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        # agent_000 deliberately NOT in manager.processes
        with patch.object(pm, "sync_next_agent_index", return_value=0):
            bid = pm.allocate_agent_id()
        assert bid == "agent_001"

    def test_loop_increments_by_one_on_collision(self, pm, manager):
        """mutmut_10/11/12: idx+=1 -> idx=1 / idx-=1 / idx+=2 on the loop-tail line.

        Force start idx=5 via patched sync and block agent_005 (in both agents
        and processes) so the otherwise '# pragma: no cover' loop-tail line runs
        exactly once. Original: idx 5->6 -> agent_006 (next index 7).
        idx=1 -> agent_001; idx-=1 -> agent_004; idx+=2 -> agent_007 -- all
        diverge from agent_006.
        """
        from provide.uterm.manager.models import AgentStatusBase

        manager.agents["agent_005"] = AgentStatusBase(agent_id="agent_005")
        manager.processes["agent_005"] = make_mock_proc()
        with patch.object(pm, "sync_next_agent_index", return_value=5):
            bid = pm.allocate_agent_id()
        assert bid == "agent_006"
        assert pm._next_agent_index == 7

    def test_next_index_advances_past_returned(self, pm, manager):
        """Guard: on the returning iteration _next_agent_index = idx + 1."""
        bid = pm.allocate_agent_id()
        assert bid == "agent_000"
        assert pm._next_agent_index == 1


# ---- cancel_spawn (2 killed, 0 equiv) ----
class TestCancelSpawnKills:
    async def test_returns_false_when_no_live_tasks(self, pm):
        """mutmut_5: `if not tasks: return False` -> `return True`.

        With no live spawn tasks the original returns False; the mutant
        returns True. Also covers the done-task-pruned path (a done task is
        not a live task, so the result is still False).
        """
        pm._spawn_tasks = []
        assert await pm.cancel_spawn() is False

        # A done task is pruned -> still treated as "no live tasks".
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        pm._spawn_tasks = [done]
        assert await pm.cancel_spawn() is False
        # _spawn_tasks reset to empty regardless.
        assert pm._spawn_tasks == []

    async def test_returns_true_when_live_tasks_cancelled(self, pm):
        """mutmut_10: final `return True` -> `return False`.

        With a live (not-done) task, the original cancels it, gathers, and
        returns True; the mutant returns False.
        """

        async def _never():
            await asyncio.Event().wait()

        live = asyncio.create_task(_never())
        # Let the task start running so it is not-done.
        await asyncio.sleep(0)
        assert not live.done()
        pm._spawn_tasks = [live]

        result = await pm.cancel_spawn()
        assert result is True
        # The live task was cancelled and the list reset.
        assert live.cancelled()
        assert pm._spawn_tasks == []


# ---- __init__ (1 killed, 0 equiv) ----
class TestInitLogDirDefault:
    def test_log_dir_default_is_empty_string(self, manager):
        """mutmut_1: log_dir default '' -> 'XXXX'.

        Constructing without log_dir must leave _log_dir as the empty string.
        With the mutant the default becomes 'XXXX', so this assertion fails.
        """
        pm = AgentProcessManager(manager)
        assert pm._log_dir == ""

    def test_log_dir_default_with_registry_omitted(self, manager):
        """Reinforce: default-arg path (no kwargs) yields empty _log_dir, not 'XXXX'."""
        pm = AgentProcessManager(manager, worker_registry={"test_game": FakeWorkerPlugin()})
        assert pm._log_dir == ""
        assert pm._log_dir != "XXXX"


# ---- sync_next_agent_index (1 killed, 0 equiv) ----
class TestSyncNextAgentIndex:
    def test_empty_with_negative_index_floors_at_zero(self, pm, manager):
        """mutmut_3: max_seen = -1 -> -2.

        With no parseable agent indices, the loop never updates max_seen, so the
        sentinel itself drives the floor: original max_seen=-1 -> max_seen+1=0,
        mutant max_seen=-2 -> max_seen+1=-1. By forcing the *current*
        _next_agent_index negative we make max() pick that sentinel-derived
        value, so the original returns 0 and the mutant returns -1.
        """
        assert manager.agents == {}
        assert manager.processes == {}
        pm._next_agent_index = -5
        result = pm.sync_next_agent_index()
        assert result == 0
        assert pm._next_agent_index == 0


# ---- _get_registry_entry (1 killed, 0 equiv) ----
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
