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
