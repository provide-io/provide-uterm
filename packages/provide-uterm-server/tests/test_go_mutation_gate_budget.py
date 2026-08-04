#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for the Go mutation gate's wall-clock budget.

``packages/provide-uterm-go/ci/mutation_gate.py`` is a CI script (outside any
coverage perimeter), so it is loaded via importlib and its pure budget logic is
exercised directly — the same approach as
``test_mutation_gate_allowlist.py``.

Why these exist: on 2026-08-03 the ``go-mutation-gate`` job ran 25 minutes and
was killed at its 20-minute limit, having printed nothing about where it was.
Its four previous runs took five minutes each, and a re-run of the identical
commit passed in 4m51s. Two things made a slow runner unreportable:

* ``run_gremlins`` ran the subprocess with no timeout, so a package that stops
  making progress takes the job down rather than failing;
* the per-package line was printed only after that package finished, so a run
  killed mid-package said nothing at all.

The gate now loses that race deliberately, and names the package.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_GATE = Path(__file__).resolve().parents[3] / "packages" / "provide-uterm-go" / "ci" / "mutation_gate.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("go_mutation_gate", _GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["go_mutation_gate"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> Any:
    return _load()


class TestRemainingBudget:
    def test_counts_down_from_the_start(self, gate: Any) -> None:
        assert gate.remaining_budget(900.0, started_at=100.0, now=100.0) == 900.0
        assert gate.remaining_budget(900.0, started_at=100.0, now=400.0) == 600.0

    def test_never_goes_negative(self, gate: Any) -> None:
        # A negative budget passed on as a subprocess timeout would raise
        # rather than fail the gate, which is a crash where a verdict belongs.
        assert gate.remaining_budget(900.0, started_at=100.0, now=5000.0) == 0.0


class TestPackageTimeout:
    def test_uses_the_package_cap_when_budget_is_ample(self, gate: Any) -> None:
        assert gate.package_timeout(300.0, budget_left=900.0) == 300.0

    def test_uses_what_is_left_when_the_budget_is_shorter(self, gate: Any) -> None:
        # The point of the budget: the last package must not be allowed to run
        # past it and hand the kill back to the job's timeout.
        assert gate.package_timeout(300.0, budget_left=45.0) == 45.0

    def test_is_zero_once_the_budget_is_spent(self, gate: Any) -> None:
        assert gate.package_timeout(300.0, budget_left=0.0) == 0.0


class TestRunGremlinsTimeout:
    def test_a_hung_package_raises_naming_itself(self, gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def hang(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="gremlins", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]

        monkeypatch.setattr(gate.subprocess, "run", hang)

        with pytest.raises(gate.GateTimeoutError) as raised:
            gate.run_gremlins("colors", coefficient=100, timeout_s=12.0)

        message = str(raised.value)
        assert "colors" in message, "a timeout that does not name the package is the report that started this"
        assert "12" in message

    def test_the_message_offers_both_readings(self, gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # A slow runner and a stuck package produce the same symptom, and the
        # gate cannot tell them apart — so it must not assert one of them.
        def hang(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="gremlins", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]

        monkeypatch.setattr(gate.subprocess, "run", hang)

        with pytest.raises(gate.GateTimeoutError) as raised:
            gate.run_gremlins("colors", coefficient=100, timeout_s=1.0)

        message = str(raised.value)
        assert "timeout-coefficient" in message
        assert "progress" in message

    def test_captured_output_survives_the_kill(self, gate: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        # Captured output is otherwise lost when the child is killed, which is
        # why the original failure had nothing to read.
        def hang(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(
                cmd="gremlins",
                timeout=float(kwargs["timeout"]),  # type: ignore[arg-type]
                output=b"mutating colors/sgr.go\n",
                stderr=b"warning: slow baseline\n",
            )

        monkeypatch.setattr(gate.subprocess, "run", hang)

        with pytest.raises(gate.GateTimeoutError):
            gate.run_gremlins("colors", coefficient=100, timeout_s=1.0)

        captured = capsys.readouterr()
        assert "mutating colors/sgr.go" in captured.out
        assert "slow baseline" in captured.err


class TestDecoded:
    def test_bytes_become_text(self, gate: Any) -> None:
        assert gate._decoded(b"hello") == "hello"

    def test_text_is_left_alone(self, gate: Any) -> None:
        assert gate._decoded("hello") == "hello"

    def test_nothing_captured_is_empty(self, gate: Any) -> None:
        assert gate._decoded(None) == ""

    def test_undecodable_bytes_do_not_raise(self, gate: Any) -> None:
        # The child was killed mid-write, so the tail can be a partial rune.
        # Losing the diagnostic to a decode error would be the worst outcome.
        assert gate._decoded(b"ok\xff") == "ok�"


class TestBudgetIsBelowTheJobTimeout:
    def test_the_default_budget_leaves_the_job_room_to_report(self, gate: Any) -> None:
        # The job is configured at timeout-minutes: 20. If the gate's budget
        # were not comfortably below that, the job would still be the thing
        # that kills the run and the diagnostic would still be lost.
        ci = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
        text = ci.read_text(encoding="utf-8")
        marker = text.index("go-mutation-gate:")
        block = text[marker : marker + 400]
        configured_minutes = int(block.split("timeout-minutes:")[1].split("\n")[0].strip())

        assert configured_minutes * 60 > gate.DEFAULT_BUDGET_S, (
            "the gate budget must expire first, or the job kills the run without a diagnostic"
        )
