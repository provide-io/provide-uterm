#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — StartSpawnSwarm, SpawnProcess, AllocateAgentId, CancelSpawn, InitLogDirDefault, SyncNextAg."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

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
        # _spawn_process was extracted into process_impl_spawn; its subprocess
        # module global lives there now.
        import provide.uterm.manager.process_impl_spawn as process_impl

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

        # _spawn_process was extracted into process_impl_spawn; its subprocess
        # module global lives there now.
        import provide.uterm.manager.process_impl_spawn as process_impl

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

        # _spawn_process was extracted into process_impl_spawn; its subprocess
        # module global lives there now.
        import provide.uterm.manager.process_impl_spawn as process_impl

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

        # _spawn_process was extracted into process_impl_spawn; its subprocess
        # module global lives there now.
        import provide.uterm.manager.process_impl_spawn as process_impl
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
        """mutmut_10: final `return True` -> `return False`; also pins the cancel
        path: a mutant that fails to ``task.cancel()`` the live task would make
        cancel_spawn's ``gather()`` wait forever on the never-completing task
        (``Event().wait()`` is not asyncio.sleep, so the conftest can't bound it).
        The ``wait_for`` turns that into a fast TimeoutError kill instead of a
        90s mutmut timeout; the ``finally`` reaps the task so it never leaks.
        """

        async def _never():
            await asyncio.Event().wait()

        live = asyncio.create_task(_never())
        # Let the task start running so it is not-done.
        await asyncio.sleep(0)
        assert not live.done()
        pm._spawn_tasks = [live]
        try:
            result = await asyncio.wait_for(pm.cancel_spawn(), timeout=2.0)
            assert result is True
            # The live task was cancelled and the list reset.
            assert live.cancelled()
            assert pm._spawn_tasks == []
        finally:
            if not live.done():
                live.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await live


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
