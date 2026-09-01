#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The goldens must be checked by someone, exactly once.

``ci/quality_checks.sh`` stands down from the goldens when
``GOLDENS_OWNED_ELSEWHERE`` is set, because running them once per matrix cell
costs four times as much for four identical answers -- they are pinned to one
interpreter. That delegation is only safe while an owner exists.

This repository has already been burned by the other outcome: the goldens check
used to print SKIP on any interpreter that was not the reference one, so
``make quality-gate`` was green on developer machines while CI's 3.13 cell was
red, and three corpora sat stale -- one hiding a Go frame type with no fields
for ``chunks_read``/``bytes_read`` at all. A skip nobody notices is worse than
the drift it avoids, so the delegation is held to its owner here.

The workflow is PARSED rather than string-searched. The first draft of this file
matched ``.ci/check_goldens.sh`` anywhere in ci.yml, which found a comment
mentioning the script four lines above the real step -- so the guard passed with
the owning step deleted, which is precisely the state it exists to catch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_WORKFLOW = _ROOT / ".github/workflows/ci.yml"
_GATE = _ROOT / "ci/quality_checks.sh"
_CHECKER = ".ci/check_goldens.sh"


def _quality_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return list(workflow["jobs"]["quality"]["steps"])


def _delegated() -> bool:
    """Whether any step in the quality job stands the gate down."""
    return any("GOLDENS_OWNED_ELSEWHERE" in (step.get("env") or {}) for step in _quality_steps())


def _owning_steps() -> list[dict[str, Any]]:
    """Steps that actually invoke the goldens checker, comments excluded."""
    return [step for step in _quality_steps() if _CHECKER in str(step.get("run", ""))]


def test_the_gate_still_knows_how_to_run_the_goldens_itself() -> None:
    """Undelegated -- a local run, or any CI without the flag -- must run them."""
    gate = _GATE.read_text(encoding="utf-8")

    assert _CHECKER in gate
    assert "GOLDENS_OWNED_ELSEWHERE" in gate


def test_delegating_the_goldens_requires_a_step_that_runs_them() -> None:
    """The whole point: if CI stands the gate down, something else must run them."""
    if not _delegated():
        return

    assert _owning_steps(), (
        "the quality job sets GOLDENS_OWNED_ELSEWHERE but no step runs "
        f"{_CHECKER}. The goldens would be checked nowhere, and every run "
        "would be green."
    )


def test_the_goldens_run_exactly_once() -> None:
    """Two owners would put the redundancy back a different way."""
    if not _delegated():
        return

    assert len(_owning_steps()) == 1


def test_the_owning_step_is_pinned_to_the_reference_interpreter() -> None:
    """One cell, and the one whose Python is already the reference version.

    Any other cell re-execs and provisions a second interpreter first, which is
    the cost this delegation exists to avoid.
    """
    if not _delegated():
        return

    condition = str(_owning_steps()[0].get("if", ""))

    assert "matrix.python-version == '3.13'" in condition, (
        "the goldens step must be pinned to one matrix cell; running it on all "
        f"four is the redundancy this delegation removes (if: {condition!r})"
    )


def test_a_failing_gate_does_not_skip_the_goldens() -> None:
    """`!cancelled()`, not a bare condition.

    quality_checks.sh deliberately runs every check before failing, so that one
    failure never hides another. A goldens step GitHub skips because an earlier
    step failed would reintroduce that masking one level up.
    """
    if not _delegated():
        return

    condition = str(_owning_steps()[0].get("if", ""))

    assert "!cancelled()" in condition, f"if: {condition!r} would be skipped when the gate fails"
