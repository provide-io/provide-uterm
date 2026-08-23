#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The sweep runner must reach every leg, whatever the ones before it did.

It used to return on the first non-zero leg. That is how the final leg came to
have never executed at all, and how two real defects sat unseen behind an
earlier failure -- each run looked like a complete one because the legs it
skipped were never mentioned.

subprocess.call is stubbed throughout: what is under test is which legs get
invoked and what the summary claims, not the suites themselves.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _ROOT / "scripts" / "run_all_tests.py"
_spec = importlib.util.spec_from_file_location("run_all_tests", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
sweep = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sweep
_spec.loader.exec_module(sweep)

_TOTAL = len(sweep._PYTEST_SUITES) + len(sweep._NPM_SUITES)


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    failing: set[int],
) -> tuple[int, list[list[str]]]:
    """Run main() with every leg stubbed; *failing* holds 0-based leg indices."""
    calls: list[list[str]] = []

    def fake_call(cmd: list[str], cwd: Any = None) -> int:
        calls.append(list(cmd))
        return 1 if (len(calls) - 1) in failing else 0

    monkeypatch.setattr(sweep.subprocess, "call", fake_call)
    monkeypatch.setattr(sys, "argv", ["run_all_tests.py", *argv])
    return sweep.main(), calls


def test_every_leg_runs_when_nothing_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, calls = _drive(monkeypatch, [], set())
    out = capsys.readouterr().out

    assert code == 0
    assert len(calls) == _TOTAL
    assert out.count("  PASS  ") == _TOTAL
    assert "All package test suites passed." in out


def test_a_failing_leg_does_not_stop_the_ones_after_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression: the old runner returned here, having run 2 legs of the lot."""
    code, calls = _drive(monkeypatch, [], {1})
    captured = capsys.readouterr()

    assert code == 1
    assert len(calls) == _TOTAL
    # The final leg is the one that had never run; name it explicitly rather
    # than trusting the count, so reordering the list cannot quietly drop it.
    assert sweep._NPM_SUITES[-1][0] == "browser consumer contract"
    assert "browser consumer contract" in captured.out
    assert captured.out.count("  FAIL  ") == 1
    assert "All package test suites passed." not in captured.out
    assert f"1 of {_TOTAL} leg(s) FAILED" in captured.err


def test_failures_in_both_halves_are_all_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Python and npm legs are separate loops; a failure in one must not end both."""
    code, calls = _drive(monkeypatch, [], {0, _TOTAL - 1})
    out = capsys.readouterr().out

    assert code == 1
    assert len(calls) == _TOTAL
    assert out.count("  FAIL  ") == 2


def test_fail_fast_stops_early_and_counts_what_it_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, calls = _drive(monkeypatch, ["--fail-fast"], {1})
    out = capsys.readouterr().out

    assert code == 1
    assert len(calls) == 2
    # Silence about the skipped legs is exactly what made a short run pass for
    # a complete one, so the count is the point of the line.
    assert f"NOT RUN {_TOTAL - 2} leg(s), stopped early" in " ".join(out.split())


def test_fail_fast_is_never_handed_to_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is our flag, not pytest's -- pytest would exit 4 on an unknown option."""
    _code, calls = _drive(monkeypatch, ["--fail-fast", "-k", "smoke"], set())

    assert all("--fail-fast" not in command for command in calls)
    assert calls[0][-2:] == ["-k", "smoke"]


def test_a_clean_fail_fast_run_reports_as_a_complete_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, calls = _drive(monkeypatch, ["--fail-fast"], set())
    out = capsys.readouterr().out

    assert code == 0
    assert len(calls) == _TOTAL
    assert "NOT RUN" not in out
    assert "All package test suites passed." in out


def test_npm_legs_do_not_receive_pytest_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-k smoke` means nothing to npm, and would be forwarded to the suite."""
    _code, calls = _drive(monkeypatch, ["-k", "smoke"], set())

    for command in calls[len(sweep._PYTEST_SUITES) :]:
        assert "-k" not in command


def test_each_leg_is_announced_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every label appears as a heading, so a hung leg is identifiable live."""
    _code, _calls = _drive(monkeypatch, [], set())
    out = capsys.readouterr().out

    for label, _uv_args, _pytest_args in sweep._PYTEST_SUITES:
        assert f"=== {label} ===" in out
    for label, _command in sweep._NPM_SUITES:
        assert f"=== {label} ===" in out


def test_a_real_subprocess_failure_is_surfaced_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_finished reports the child's own exit code, not a normalized 1."""
    monkeypatch.setattr(sweep.subprocess, "call", lambda *_a, **_k: 5)
    monkeypatch.setattr(sys, "argv", ["run_all_tests.py", "--fail-fast"])

    code = sweep.main()
    captured = capsys.readouterr()

    assert code == 1
    # pytest's exit 5 is "no tests collected" -- a distinct failure worth
    # keeping visible, even though the process itself exits 1.
    assert "(exit 5)" in captured.err
    assert "(exit 5)" in captured.out


def test_the_script_still_runs_as_a_program() -> None:
    """--help-less by design, but it must at least be importable and executable."""
    result = subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(_SCRIPT_PATH)!r})"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=_ROOT,
    )

    # runpy executes it as __main__ under a name that is not "__main__" for the
    # guard, so nothing runs -- this only proves the module has no import-time
    # error, which a syntax slip in the leg tables would produce.
    assert result.returncode == 0, result.stderr
