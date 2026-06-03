#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — LoadWorkerType, KillAgentExtra, ReleaseAgentAccount, LaunchQueuedAgentLogging."""

from __future__ import annotations

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
