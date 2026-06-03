#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — MonitorProcessesMonitorLoop, BuildWorkerEnv, ScopeWorkerTokens, NoteAgentId."""

from __future__ import annotations

import contextlib
import os
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
