#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — BuildPreexecRlimitFn."""

from __future__ import annotations

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
