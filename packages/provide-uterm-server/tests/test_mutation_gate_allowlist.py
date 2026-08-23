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

    def test_changed_only_support_change_defers_the_full_perimeter(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A support change used to start the perimeter here; now it hands it off.

        It previously called run_mutation_gate with source_paths=None -- one
        mutmut invocation over all 38 perimeter files. That cannot finish inside
        the job's 90-minute cap and was never a verdict, only a timeout. The gate
        now prints the marker and returns, and ci/prepare_mutation_args.sh
        dispatches mutation-full.yml, which fans the same targets across a matrix.
        """
        calls: list[object] = []

        monkeypatch.setattr(
            sys,
            "argv",
            ["run_mutation_gate.py", "--changed-only", "--base-ref", "origin/main", "--retries", "0"],
        )
        monkeypatch.setattr(gate, "_changed_python_paths", lambda _base, _staged, _roots: [])
        monkeypatch.setattr(gate, "_changed_paths", lambda _base, _staged: ["mutation_equivalents.toml"])
        monkeypatch.setattr(gate, "_half_cpu_count", lambda: 1)
        monkeypatch.setattr(gate, "run_mutation_gate", lambda *a, **k: calls.append((a, k)))

        assert gate.main() == 0
        assert calls == [], "the un-chunked full perimeter was started; it can only time out"
        out = capsys.readouterr().out
        assert "mutation gate full-perimeter trigger" in out
        assert gate.FULL_PERIMETER_REQUIRED_MARKER in out


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


# ---------------------------------------------------------------------------
# run_mutation_gate: results must be read before the config is restored
# ---------------------------------------------------------------------------


class TestNarrowedResultsAreReadBeforeRestore:
    """``mutmut results`` reads source_paths from the ROOT pyproject too.

    A ``--paths`` / ``--changed-only`` run narrows that config for the duration of
    the run, so the results have to be asked for while the narrowed config is
    still in place. Restoring first made mutmut report on the DEFAULT perimeter
    instead of the files just mutated, which answers "no results" — and the gate
    cannot tell that from a file with no mutable surface, so it passed. A
    ``--paths`` run over router_impl.py that mutmut itself tallied as 976 mutants
    with 456 survivors was read back as ``total=0`` and passed as clean.
    """

    def test_collect_stats_sees_the_narrowed_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        narrowed = "src/provide/uterm/only_this.py"
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mutmut]\nsource_paths = ["src/provide/uterm/original.py"]\n', encoding="utf-8"
        )
        seen: list[str] = []

        def _fake_collect(*_args: object, **_kwargs: object) -> tuple[list[str], dict[str, int]]:
            seen.append((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
            return [], {"total": 1, "killed": 1, "bad_total": 0}

        monkeypatch.setattr(gate, "_collect_stats", _fake_collect)
        monkeypatch.setattr(gate, "_seed_mutants_config", lambda **_k: None)
        monkeypatch.setattr(gate, "_scoped_test_selection", lambda _p: ())
        monkeypatch.setattr(gate, "_load_equivalent_allowlist", lambda *_a, **_k: {})
        monkeypatch.setattr(gate.subprocess, "run", lambda *_a, **_k: type("R", (), {"returncode": 0})())

        gate.run_mutation_gate(None, 1, 0, 100.0, source_paths=[narrowed])

        assert seen, "stats were never collected"
        assert narrowed in seen[0], (
            "mutmut results was asked AFTER pyproject.toml had been restored, so it "
            "reported on the default perimeter rather than the narrowed target"
        )

    def test_config_is_restored_afterwards(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        original = '[tool.mutmut]\nsource_paths = ["src/provide/uterm/original.py"]\n'
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")

        monkeypatch.setattr(gate, "_collect_stats", lambda *_a, **_k: ([], {"total": 1, "killed": 1, "bad_total": 0}))
        monkeypatch.setattr(gate, "_seed_mutants_config", lambda **_k: None)
        monkeypatch.setattr(gate, "_scoped_test_selection", lambda _p: ())
        monkeypatch.setattr(gate, "_load_equivalent_allowlist", lambda *_a, **_k: {})
        monkeypatch.setattr(gate.subprocess, "run", lambda *_a, **_k: type("R", (), {"returncode": 0})())

        gate.run_mutation_gate(None, 1, 0, 100.0, source_paths=["src/provide/uterm/only_this.py"])

        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# main(): a support-file change defers instead of running the perimeter
# ---------------------------------------------------------------------------


class TestSupportFileChangeDefersToTheFullWorkflow:
    """A support-file change is a perimeter-wide question this job cannot answer.

    The perimeter is 38 files that mutmut has to be given one at a time, so
    answering it in the changed-only job means hours against a 90-minute cap --
    what that produced was never a verdict, only a timeout. The gate prints the
    marker instead, and ci/prepare_mutation_args.sh dispatches mutation-full.yml,
    where the same 38 targets run across a matrix.
    """

    def _run_main(self, monkeypatch: pytest.MonkeyPatch, support: list[str]) -> tuple[int, bool]:
        ran = False

        def _fake_run(*_args: object, **_kwargs: object) -> dict[str, int]:
            nonlocal ran
            ran = True
            return {"total": 0, "bad_total": 0}

        monkeypatch.setattr(gate, "_changed_python_paths", lambda *_a, **_k: [])
        monkeypatch.setattr(gate, "_changed_paths", lambda *_a, **_k: list(support))
        monkeypatch.setattr(gate, "_changed_mutation_support_paths", lambda _p: list(support))
        monkeypatch.setattr(gate, "run_mutation_gate", _fake_run)
        monkeypatch.setattr(sys, "argv", ["run_mutation_gate.py", "--changed-only", "--base-ref", "HEAD~1"])
        return gate.main(), ran

    def test_marker_is_printed_and_the_perimeter_is_not_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, ran = self._run_main(monkeypatch, ["scripts/run_mutation_gate.py"])
        out = capsys.readouterr().out
        assert rc == 0
        assert gate.FULL_PERIMETER_REQUIRED_MARKER in out
        assert not ran, "the un-chunked full perimeter was started; it can only time out"

    def test_no_support_change_skips_without_the_marker(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, ran = self._run_main(monkeypatch, [])
        out = capsys.readouterr().out
        assert rc == 0
        assert gate.FULL_PERIMETER_REQUIRED_MARKER not in out
        assert "skipped" in out
        assert not ran


# ---------------------------------------------------------------------------
# Carrying a proven kill across attempts
# ---------------------------------------------------------------------------


class TestProvenKillsSurviveALaterTimeout:
    """A kill proved by one attempt must not be undone by the next attempt's timeout.

    mutmut orders a mutant's covering tests by measured duration, fastest first.
    At microsecond scale that is noise, so when the covering set holds a test
    that spins on the mutant, whether the killing test runs first is a coin flip
    per attempt. Reading only the last attempt therefore discarded exactly what
    the retry was run to find out: two attempts of one control_channel.py run
    reported different timeout sets, each killing a mutant the other did not.
    """

    def test_a_timeout_is_restored_to_the_kill_it_was_shown_to_be(self) -> None:
        promoted = gate._promote_proven_kills([("a", "killed"), ("flaky", "timeout")], {"flaky"})
        assert promoted == [("a", "killed"), ("flaky", "killed")]

    def test_a_survived_verdict_is_never_promoted(self) -> None:
        """``survived`` means a test ran to completion and passed.

        That contradicts the earlier kill instead of failing to reach it, so it
        is test flakiness and has to stay visible. Only the absence of a verdict
        is restored.
        """
        promoted = gate._promote_proven_kills([("flaky", "survived")], {"flaky"})
        assert promoted == [("flaky", "survived")]

    def test_an_unproven_timeout_still_fails_the_gate(self) -> None:
        promoted = gate._promote_proven_kills([("slow", "timeout")], {"other"})
        assert promoted == [("slow", "timeout")]

    def test_collect_stats_counts_a_carried_kill_as_killed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            gate,
            "_results_per_mutant",
            lambda _pv, _env: [("a", "killed"), ("flaky", "timeout")],
        )
        _effective, stats = gate._collect_stats(None, {}, {}, {"flaky"})
        assert stats["killed"] == 2
        assert stats["timeout"] == 0
        assert stats["bad_total"] == 0

    def test_the_gate_passes_once_both_attempts_together_kill_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end shape: neither attempt is clean, the pair is.

        Attempt 1 kills ``x`` and times out on ``y``; attempt 2 does the
        reverse. Before this, attempt 2's stats were the verdict and the run
        failed on ``x`` — a mutant attempt 1 had already killed.
        """
        # chdir first: run_mutation_gate rmtree's ./mutants and rewrites
        # ./pyproject.toml, so running it against the real repo root would
        # delete a live mutants tree.
        monkeypatch.chdir(tmp_path)
        results = iter(
            [
                [("x", "killed"), ("y", "timeout")],
                [("x", "timeout"), ("y", "killed")],
            ]
        )
        monkeypatch.setattr(gate, "_results_per_mutant", lambda _pv, _env: next(results))
        monkeypatch.setattr(gate, "_seed_mutants_config", lambda **_k: None)
        monkeypatch.setattr(gate, "_scoped_test_selection", lambda _p: ())
        monkeypatch.setattr(gate, "_load_equivalent_allowlist", lambda *_a, **_k: {})
        monkeypatch.setattr(gate.subprocess, "run", lambda *_a, **_k: type("R", (), {"returncode": 1})())

        stats = gate.run_mutation_gate(None, 4, 1, 100.0, source_paths=["src/x.py"])
        assert stats["killed"] == 2
        assert stats["bad_total"] == 0
