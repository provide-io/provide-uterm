#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The parity runner has to be right about the workflow, or it is worse than nothing.

A tool that reports "every reproducible step passed" while quietly blanking an
expression, or silently assuming a conditional step would have run, hands back
the same false confidence the local commands already gave. So the two refusals
are tested as behaviour, not documented as intent.

These run against the real ``.github/workflows/ci.yml`` rather than a fixture
wherever the assertion is about the repo, so the tests fail if the workflow
grows an expression form the runner cannot resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import ci_parity


def _context(**overrides: str) -> dict[str, str]:
    return {"matrix.python-version": "3.13", "github.event_name": "push", **overrides}


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_an_unknown_expression_is_an_error_not_a_blank() -> None:
    """The whole point: a blank silently changes the command it lands in."""
    with pytest.raises(ci_parity.UnresolvedError, match="github.token"):
        ci_parity._substitute("gh api --token ${{ github.token }}", _context())


def test_an_unevaluatable_condition_marks_the_step_rather_than_running_it() -> None:
    job = {"steps": [{"name": "obscure", "if": "hashFiles('x') != ''", "run": "echo hi"}]}

    resolved = ci_parity._steps_for(job, _context())

    assert [step["kind"] for step in resolved] == ["unevaluated"]


def test_one_unresolvable_step_does_not_cost_the_others() -> None:
    """A partial run honestly described beats a whole run abandoned."""
    job = {
        "steps": [
            {"name": "needs a token", "run": "gh api ${{ github.token }}"},
            {"name": "does not", "run": "pytest -q"},
        ]
    }

    resolved = ci_parity._steps_for(job, _context())

    assert [step["kind"] for step in resolved] == ["unevaluated", "run"]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_the_matrix_value_reaches_the_command() -> None:
    job = {"steps": [{"run": "uv run --python ${{ matrix.python-version }} pytest"}]}

    resolved = ci_parity._steps_for(job, _context())

    assert resolved[0]["command"] == "uv run --python 3.13 pytest"


@pytest.mark.parametrize(
    ("event", "expected"),
    [("schedule", "deep"), ("push", "ci")],
)
def test_the_ternary_resolves_through_the_same_evaluator_as_an_if(event: str, expected: str) -> None:
    """One evaluator, so a condition cannot mean two things in one workflow."""
    text = "${{ github.event_name == 'schedule' && 'deep' || 'ci' }}"

    assert ci_parity._substitute(text, _context(**{"github.event_name": event})) == expected


def test_a_step_gated_on_another_matrix_cell_is_skipped() -> None:
    job = {"steps": [{"name": "goldens", "if": "matrix.python-version == '3.13'", "run": "bash x.sh"}]}

    on_reference = ci_parity._steps_for(job, _context())
    elsewhere = ci_parity._steps_for(job, _context(**{"matrix.python-version": "3.11"}))

    assert on_reference[0]["kind"] == "run"
    assert elsewhere[0]["kind"] == "skipped"


def test_not_cancelled_is_treated_as_reached() -> None:
    """`!cancelled()` guards a step from an EARLIER failure, which locally has
    not happened -- the run stops at the first failing command."""
    assert ci_parity._evaluate_if("${{ !cancelled() && matrix.python-version == '3.13' }}", _context())
    assert not ci_parity._evaluate_if("failure()", _context())


# ---------------------------------------------------------------------------
# Against the real workflow
# ---------------------------------------------------------------------------


def _resolvable_context(job: dict[str, Any], temp: Path) -> dict[str, str]:
    """The job's context with the git-derived keys guaranteed present.

    ``github.event.before`` comes from ``HEAD~1``, which does not exist in a
    depth-1 checkout -- and CI checks this repo out at depth 1. That absence is
    a fact about the clone, not an expression form the runner cannot handle, so
    filling it keeps this test measuring the thing it names. The runner itself
    still refuses rather than substituting a blank; that refusal is pinned by
    ``test_an_unknown_expression_is_an_error_not_a_blank``.
    """
    context = ci_parity._context_for(job, {}, temp)
    context.setdefault("github.event.before", "0" * 40)
    context.setdefault("github.ref_name", "main")
    return context


def test_every_job_in_the_workflow_resolves_on_its_first_matrix_cell() -> None:
    """The regression guard: a new expression form must not go unnoticed.

    A step the runner cannot resolve is reported rather than run, so the
    failure mode is a quiet partial run. This makes it loud instead, at the one
    moment someone is in a position to fix it -- when they add the expression.
    """
    jobs = ci_parity._load_jobs()
    temp = _ROOT / ".ci-parity-tmp"

    unresolved: list[str] = []
    for name, job in jobs.items():
        context = _resolvable_context(job, temp)
        for step in ci_parity._steps_for(job, context):
            if step["kind"] == "unevaluated" and "github.token" not in step["why"]:
                unresolved.append(f"{name}: {step['name']} -- {step['why']}")

    assert not unresolved, "the parity runner cannot resolve:\n" + "\n".join(unresolved)


def test_the_quality_job_still_carries_the_commands_it_is_relied_on_for() -> None:
    """Anchors the tool against the real file rather than only a fixture."""
    jobs = ci_parity._load_jobs()
    context = ci_parity._context_for(jobs["quality"], {"python-version": "3.13"}, _ROOT / ".ci-parity-tmp")
    commands = [step["command"] for step in ci_parity._steps_for(jobs["quality"], context) if step["kind"] == "run"]

    assert any("ci/quality_checks.sh" in command for command in commands)
    assert any("run_pytest_gate.py" in command for command in commands)


def test_a_shallow_clone_omits_the_git_key_rather_than_blanking_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI checks out at depth 1, so ``HEAD~1`` is unavailable there.

    A changed-only gate handed an empty ``--since`` reads it as "everything" or
    "nothing" depending on the gate -- both wrong, and both silent. So the key
    is left out and the step is reported instead of run.
    """
    monkeypatch.setattr(ci_parity, "_git", lambda *args: None)
    job = {"steps": [{"name": "changed-only", "run": "gate --since ${{ github.event.before }}"}]}

    context = ci_parity._context_for(job, {}, _ROOT / ".ci-parity-tmp")

    assert "github.event.before" not in context
    assert ci_parity._steps_for(job, context)[0]["kind"] == "unevaluated"
