#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for the C# mutation gate's wall-clock budget.

``packages/provide-uterm-csharp/ci/stryker_gate.py`` is a CI script (outside any
coverage perimeter), so it is loaded via importlib and its pure budget logic is
exercised directly — the same approach as ``test_go_mutation_gate_budget.py``.

Why these exist: ``stryker-config.json`` sets ``additional-timeout`` to 30
minutes. That is deliberate — a mutation inside a ``static readonly``
initializer has no per-test coverage for Stryker to derive a leash from, so it
is measured against the whole ~12m36s suite, and under the previous 60s
allowance a random slice of ServerConfig/Load.cs's 211 static mutants timed out
every run (38, then 52, overlapping on only 9) despite all 211 killing cleanly
on 2026-08-12.

A 30-minute leash needs a backstop, or one stuck mutant quietly spends the
job's whole 120 minutes and GitHub reports "cancelled" with nothing to read.
These tests pin that the gate's own budget stays below the job's limit, and
that a run which outruns it fails with a message naming both readings.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GATE = _REPO / "packages" / "provide-uterm-csharp" / "ci" / "stryker_gate.py"
_WORKFLOW = _REPO / ".github" / "workflows" / "ci.yml"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("csharp_stryker_gate", _GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["csharp_stryker_gate"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> Any:
    return _load()


class TestBudgetOrdering:
    def test_the_gate_budget_is_below_the_job_timeout(self, gate: Any) -> None:
        # The whole point: the gate must lose the race first, so the failure
        # names where it was instead of arriving as a bare "cancelled".
        text = _WORKFLOW.read_text(encoding="utf-8")
        block = text.split("csharp-mutation-gate:", 1)[1]
        minutes = int(re.search(r"timeout-minutes:\s*(\d+)", block).group(1))  # type: ignore[union-attr]
        assert minutes * 60 > gate.DEFAULT_BUDGET_S, (
            f"gate budget {gate.DEFAULT_BUDGET_S}s must stay under the job's {minutes}m limit"
        )

    def test_the_budget_still_covers_a_full_perimeter_run(self, gate: Any) -> None:
        # A full perimeter run measured 53 minutes locally once the static
        # key-set mutants were excluded. Two earlier budgets (6000s, 6900s) were
        # sized against slower configurations and killed honest runs, producing
        # no report at all — so this floor is deliberately well above the
        # measurement, not tight to it.
        assert gate.DEFAULT_BUDGET_S >= 90 * 60


class TestRunStrykerTimeout:
    def test_a_hung_run_raises_naming_its_scope(self, gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def hang(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="stryker", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]

        monkeypatch.setattr(gate.subprocess, "run", hang)

        with pytest.raises(gate.GateTimeoutError) as raised:
            gate.run_stryker(["**/ServerConfig/Load.cs"], budget_s=12.0)

        message = str(raised.value)
        assert "ServerConfig/Load.cs" in message, "a timeout that does not name its scope is unreportable"
        assert "12" in message

    def test_the_full_perimeter_run_is_named_too(self, gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def hang(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="stryker", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]

        monkeypatch.setattr(gate.subprocess, "run", hang)

        with pytest.raises(gate.GateTimeoutError) as raised:
            gate.run_stryker(None, budget_s=5.0)

        assert "the full perimeter" in str(raised.value)

    def test_the_message_offers_both_readings(self, gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # A slow runner and a stuck mutant produce the same symptom, and the
        # gate cannot tell them apart — so it must not assert one of them.
        def hang(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="stryker", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]

        monkeypatch.setattr(gate.subprocess, "run", hang)

        with pytest.raises(gate.GateTimeoutError) as raised:
            gate.run_stryker(None, budget_s=1.0)

        message = str(raised.value)
        assert "no progress" in message
        assert "additional-timeout" in message
