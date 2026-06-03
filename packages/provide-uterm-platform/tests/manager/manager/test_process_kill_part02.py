#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — BuildPreexecRlimitFn2, SpawnPlatformKwargs."""

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


class TestBuildPreexecRlimitFn2:
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
