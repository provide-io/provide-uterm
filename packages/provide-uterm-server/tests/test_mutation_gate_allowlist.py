#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for the mutation gate's equivalent-mutant allowlist.

``scripts/run_mutation_gate.py`` is a CI script (outside any coverage perimeter),
so it is loaded via importlib and its pure allowlist logic is exercised directly.
Covers:
  - ``_load_equivalent_allowlist``: TOML parse, missing file, missing/empty
    mutant or reason (must raise — equivalence MUST be justified).
  - ``_state_counts_from``: state tally.
  - ``_apply_equivalent_allowlist``: only ``survived``/``suspicious`` mutants are
    excusable; allowlisted-but-killed / absent / no-tests entries are reported
    stale and never excused; an empty allowlist is a no-op (backward compat).

Each test fails if the behaviour regresses (asserts the partition + counts, not
just absence of an exception).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "run_mutation_gate.py"
_spec = importlib.util.spec_from_file_location("run_mutation_gate", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# _load_equivalent_allowlist
# ---------------------------------------------------------------------------


class TestLoadEquivalentAllowlist:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert gate._load_equivalent_allowlist(tmp_path / "nope.toml") == {}

    def test_empty_equivalent_array_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "eq.toml"
        f.write_text("equivalent = []\n")
        assert gate._load_equivalent_allowlist(f) == {}

    def test_valid_entries_parsed(self, tmp_path: Path) -> None:
        f = tmp_path / "eq.toml"
        f.write_text(
            '[[equivalent]]\nmutant = "mod.x_f__mutmut_1"\nreason = "operand unread before overwrite"\n'
            '[[equivalent]]\nmutant = "mod.x_g__mutmut_4"\nreason = "codec name case flip on ascii"\n'
        )
        loaded = gate._load_equivalent_allowlist(f)
        assert loaded == {
            "mod.x_f__mutmut_1": "operand unread before overwrite",
            "mod.x_g__mutmut_4": "codec name case flip on ascii",
        }

    def test_missing_reason_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "eq.toml"
        f.write_text('[[equivalent]]\nmutant = "mod.x_f__mutmut_1"\n')
        with pytest.raises(RuntimeError, match="non-empty 'mutant' and 'reason'"):
            gate._load_equivalent_allowlist(f)

    def test_missing_mutant_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "eq.toml"
        f.write_text('[[equivalent]]\nreason = "some reason"\n')
        with pytest.raises(RuntimeError, match="non-empty 'mutant' and 'reason'"):
            gate._load_equivalent_allowlist(f)

    def test_empty_reason_string_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "eq.toml"
        f.write_text('[[equivalent]]\nmutant = "mod.x_f__mutmut_1"\nreason = ""\n')
        with pytest.raises(RuntimeError, match="non-empty 'mutant' and 'reason'"):
            gate._load_equivalent_allowlist(f)


# ---------------------------------------------------------------------------
# _state_counts_from
# ---------------------------------------------------------------------------


class TestStateCountsFrom:
    def test_tallies_states(self) -> None:
        mutants = [("a", "killed"), ("b", "killed"), ("c", "survived"), ("d", "no tests")]
        assert gate._state_counts_from(mutants) == {"killed": 2, "survived": 1, "no tests": 1}

    def test_empty(self) -> None:
        assert gate._state_counts_from([]) == {}


# ---------------------------------------------------------------------------
# _apply_equivalent_allowlist
# ---------------------------------------------------------------------------


class TestApplyEquivalentAllowlist:
    def test_empty_allowlist_is_noop(self) -> None:
        mutants = [("a", "killed"), ("b", "survived")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {})
        assert effective == mutants
        assert excused == []
        assert stale == []

    def test_allowlisted_survivor_is_excused(self) -> None:
        mutants = [("a", "killed"), ("b", "survived")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {"b": "equivalent"})
        assert effective == [("a", "killed")]
        assert excused == ["b"]
        assert stale == []

    def test_allowlisted_suspicious_is_excused(self) -> None:
        mutants = [("a", "suspicious")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {"a": "equivalent"})
        assert effective == []
        assert excused == ["a"]

    def test_non_allowlisted_survivor_stays(self) -> None:
        mutants = [("a", "survived")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {"other": "x"})
        assert effective == [("a", "survived")]
        assert excused == []
        assert stale == ["other"]

    def test_allowlisted_killed_is_not_excused_and_is_stale(self) -> None:
        # An allowlisted mutant that is now KILLED must NOT be excused (it counts
        # as killed); the entry is reported stale so it can be cleaned up.
        mutants = [("a", "killed")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {"a": "equivalent"})
        assert effective == [("a", "killed")]
        assert excused == []
        assert stale == ["a"]

    def test_allowlisted_no_tests_is_not_excused(self) -> None:
        # "no tests" is a coverage gap, not equivalence — never excusable.
        mutants = [("a", "no tests")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {"a": "equivalent"})
        assert effective == [("a", "no tests")]
        assert excused == []
        assert stale == ["a"]

    def test_allowlisted_timeout_is_excused(self) -> None:
        # A documented-equivalent mutant is unkillable, so a CI wall-clock timeout
        # on it is timing noise (same fact as "survived"), not a coverage gap.
        mutants = [("a", "timeout")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {"a": "equivalent"})
        assert effective == []
        assert excused == ["a"]
        assert stale == []

    def test_non_allowlisted_timeout_stays(self) -> None:
        # A timeout on a mutant with no documented equivalence is a hard failure
        # (it could be hiding a real kill gap) — never excused.
        mutants = [("a", "timeout")]
        effective, excused, stale = gate._apply_equivalent_allowlist(mutants, {})
        assert effective == [("a", "timeout")]
        assert excused == []

    def test_allowlisted_absent_entirely_is_stale(self) -> None:
        effective, excused, stale = gate._apply_equivalent_allowlist([("a", "killed")], {"ghost": "x"})
        assert excused == []
        assert stale == ["ghost"]

    def test_excusing_drops_score_denominator(self) -> None:
        # 1 killed + 1 equivalent survivor -> after excusing, effective is all killed.
        mutants = [("a", "killed"), ("equiv", "survived")]
        effective, _excused, _stale = gate._apply_equivalent_allowlist(mutants, {"equiv": "equivalent"})
        counts = gate._state_counts_from(effective)
        total = sum(counts.values())
        killed = counts.get("killed", 0)
        assert total == 1
        assert killed == 1
        assert killed == total  # would be the killed==100 pass condition


# ---------------------------------------------------------------------------
# _collect_stats + the transient-timeout guard contract
# ---------------------------------------------------------------------------


class TestCollectStatsTimeoutContract:
    """Lock the asymmetric timeout-excusal contract.

    mutmut flags ``timeout`` purely on wall-clock and it flakes on a loaded CI
    runner. The gate treats a timeout as a hard failure for a NON-allowlisted
    mutant (it could hide a real kill gap), but excuses it for an ALLOWLISTED —
    therefore proven-unkillable — mutant, where a timeout is the same fact as
    "survived" surfaced by CI timing (a now-killable mutant fails fast, never
    times out). ``_collect_stats`` must reflect both halves.
    """

    def test_non_allowlisted_timeout_counts_as_bad(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            gate,
            "_results_per_mutant",
            lambda _pv, _env: [("a", "killed"), ("b", "killed"), ("slow", "timeout")],
        )
        effective, stats = gate._collect_stats(None, {}, {})
        assert stats["timeout"] == 1
        assert stats["bad_total"] == 1
        assert stats["killed"] == 2
        # The un-excused timeout stays in `effective` and blocks the gate.
        assert ("slow", "timeout") in effective

    def test_allowlisted_timeout_is_excused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A documented-equivalent (allowlisted) mutant is unkillable, so a CI
        # wall-clock timeout on it is timing noise, not a coverage gap -> excused.
        monkeypatch.setattr(
            gate,
            "_results_per_mutant",
            lambda _pv, _env: [("a", "killed"), ("slow", "timeout")],
        )
        effective, stats = gate._collect_stats(None, {}, {"slow": "documented-equivalent"})
        assert stats["bad_total"] == 0
        assert stats["timeout"] == 0
        assert stats["killed"] == 1
        assert stats["total"] == 1  # excused mutant dropped from the denominator
        assert ("slow", "timeout") not in effective

    def test_clean_run_has_no_bad(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            gate,
            "_results_per_mutant",
            lambda _pv, _env: [("a", "killed"), ("b", "killed")],
        )
        _effective, stats = gate._collect_stats(None, {}, {})
        assert stats["bad_total"] == 0
        assert stats["killed"] == 2
        assert stats["total"] == 2


# ---------------------------------------------------------------------------
# --changed-only support-file handling
# ---------------------------------------------------------------------------


class TestChangedOnlySupportFiles:
    def test_mutation_allowlist_change_is_support_change(self) -> None:
        changed = ["mutation_equivalents.toml"]
        assert gate._changed_mutation_support_paths(changed) == ["mutation_equivalents.toml"]

    def test_mutation_config_change_is_support_change(self) -> None:
        changed = ["pyproject.toml"]
        assert gate._changed_mutation_support_paths(changed) == ["pyproject.toml"]

    def test_bound_mutation_test_change_is_support_change(self) -> None:
        changed = ["packages/provide-uterm/tests/test_control_channel_patterns.py"]
        assert gate._changed_mutation_support_paths(changed) == [
            "packages/provide-uterm/tests/test_control_channel_patterns.py"
        ]

    def test_changed_only_support_change_runs_full_perimeter(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[dict[str, object]] = []

        monkeypatch.setattr(
            sys,
            "argv",
            ["run_mutation_gate.py", "--changed-only", "--base-ref", "origin/main", "--retries", "0"],
        )
        monkeypatch.setattr(gate, "_changed_python_paths", lambda _base, _staged, _roots: [])
        monkeypatch.setattr(gate, "_changed_paths", lambda _base, _staged: ["mutation_equivalents.toml"])
        monkeypatch.setattr(gate, "_half_cpu_count", lambda: 1)

        def fake_run_mutation_gate(
            python_version: str | None,
            max_children: int,
            retries: int,
            min_mutation_score: float,
            source_paths: list[str] | None = None,
            allow_empty: bool = False,
        ) -> dict[str, int]:
            calls.append(
                {
                    "python_version": python_version,
                    "max_children": max_children,
                    "retries": retries,
                    "min_mutation_score": min_mutation_score,
                    "source_paths": source_paths,
                    "allow_empty": allow_empty,
                }
            )
            return {"total": 1, "killed": 1, "bad_total": 0}

        monkeypatch.setattr(gate, "run_mutation_gate", fake_run_mutation_gate)

        assert gate.main() == 0
        assert calls == [
            {
                "python_version": "3.11",
                "max_children": 1,
                "retries": 0,
                "min_mutation_score": 100.0,
                "source_paths": None,
                "allow_empty": False,
            }
        ]
        assert "mutation gate full-perimeter trigger" in capsys.readouterr().out


class TestScopedMutationSelection:
    def test_process_manager_paths_use_dedicated_tests(self) -> None:
        selected = gate._scoped_test_selection(["src/provide/uterm/manager/process_impl.py"])
        assert selected is not None
        assert "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part01.py" in selected
        assert "packages/provide-uterm/tests/bridge/test_coordinator_stress.py" not in selected

    def test_bridge_hub_paths_use_dedicated_tests_without_stress(self) -> None:
        selected = gate._scoped_test_selection(
            [
                "src/provide/uterm/server/bridge/hub/presence.py",
                "src/provide/uterm/server/bridge/hub/store.py",
                "src/provide/uterm/server/bridge/hub/polling_service.py",
                "src/provide/uterm/deckmux/_service.py",
                "src/provide/uterm/bridge/schemas.py",
                "src/provide/uterm/bridge/coordinator.py",
            ]
        )

        assert selected is not None
        assert "packages/provide-uterm/tests/bridge/test_coordinator_units.py" in selected
        assert "packages/provide-uterm-server/tests/bridge/hub/test_store_kill.py" in selected
        assert "packages/provide-uterm/tests/bridge/test_coordinator_stress.py" not in selected

    def test_bridge_coordinator_path_uses_dedicated_tests_without_stress(self) -> None:
        selected = gate._scoped_test_selection(["src/provide/uterm/bridge/coordinator.py"])

        assert selected is not None
        assert "packages/provide-uterm/tests/bridge/test_coordinator_units.py" in selected
        assert "packages/provide-uterm/tests/bridge/test_coordinator_stress.py" not in selected

    def test_unscoped_path_keeps_full_selection(self) -> None:
        assert gate._scoped_test_selection(["src/provide/uterm/io.py"]) is None
