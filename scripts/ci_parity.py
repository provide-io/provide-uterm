#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Run locally what a named CI job actually runs.

The static gate has had one source of truth for a while (``ci/quality_checks.sh``,
shared by CI and ``make quality-gate``). The *test* invocations never did: they
live in the workflow, spread across jobs and matrix cells, each with its own
flags. Reproducing one from memory means reproducing a narrower version of it,
and a narrower check passes when the real one would not.

That is not hypothetical. In a single session, four pushes went red because the
local command was a subset of the CI one:

* ``pytest <one file>`` where CI collects every testpath -- and the failure was
  a top-level package name two testpaths were contesting, which cannot occur
  when only one is collected.
* ``vitest run`` where CI runs ``vitest run --coverage`` -- and the workspace
  enforces a 100% floor only under ``--coverage``.
* ``pytest .../tests/bridge/hub/`` where CI runs the whole server suite -- and
  the test that broke lived one directory up.
* the module's own tests where CI additionally runs ``-m playwright``.

So this reads the workflow rather than restating it. Nothing here is a copy of
a CI command; every command is parsed out of ``.github/workflows/ci.yml`` at the
moment you run it, which is the only version that cannot drift.

Usage::

    scripts/ci_parity.py --list
    scripts/ci_parity.py quality --python 3.13 --print
    scripts/ci_parity.py quality --python 3.13
    scripts/ci_parity.py npm-quality
    scripts/ci_parity.py quality --python 3.13 --with-setup   # provision too

Two deliberate refusals, both because a parity tool that guesses is worse than
none:

* An expression it cannot resolve is an error naming the expression, never an
  empty string substituted into a command.
* An ``if:`` it cannot evaluate skips the step and says so on stderr, rather
  than assuming the step would have run.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github/workflows/ci.yml"

#: Expression fragments resolvable without a GitHub context. ``runner.temp``
#: gets a real local directory because steps write artifacts into it.
_STATIC_CONTEXT = {
    "runner.os": {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows_NT"}.get(os.uname().sysname, "Linux"),
    "github.run_attempt": "1",
    "github.run_id": "0",
    "github.workflow": "CI",
    "github.event_name": "push",
    "github.ref_name": "main",
    "github.base_ref": "",
    "github.event.repository.default_branch": "main",
    # Only present if the developer already exports one. Absent is left
    # UNRESOLVED rather than blank: a step handed an empty token does not fail
    # the way the real one does, it fails differently and confusingly.
    **({"github.token": os.environ["GH_TOKEN"]} if os.environ.get("GH_TOKEN") else {}),
}

#: ``<condition> && 'a' || 'b'`` -- GitHub's ternary, used for the hypothesis
#: profile. Resolved through the same evaluator as an ``if:`` so the two cannot
#: disagree about what a condition means.
_TERNARY = re.compile(r"^(?P<cond>.+?)\s*&&\s*'(?P<yes>[^']*)'\s*\|\|\s*'(?P<no>[^']*)'$")

#: Steps that provision rather than verify. SKIPPED unless --with-setup, and
#: named in the output either way so a skip is never silent.
#:
#: Opt-in rather than opt-out because CI provisions a throwaway runner and your
#: machine is not one. `uv sync --frozen --group dev` is the example that
#: proved it: run locally it resolves the ROOT project only and UNINSTALLS the
#: workspace members' own dependencies, leaving an env whose test collection
#: fails with ModuleNotFoundError. Correct on a runner, destructive here.
_SETUP_MARKERS = ("npm ci", "uv sync", "playwright install", "npm run build:frontend")


class UnresolvedError(RuntimeError):
    """An expression or condition the tool refuses to guess at."""


def _load_jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return dict(workflow["jobs"])


def _matrix_of(job: dict[str, Any]) -> dict[str, list[Any]]:
    matrix = job.get("strategy", {}).get("matrix", {})
    return {key: value for key, value in matrix.items() if isinstance(value, list)}


def _substitute(text: str, context: dict[str, str]) -> str:
    """Replace every ``${{ ... }}``, refusing rather than blanking an unknown."""

    def _one(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if expression in context:
            return context[expression]
        ternary = _TERNARY.match(expression)
        if ternary is not None:
            taken = _evaluate_if(ternary.group("cond"), context)
            return ternary.group("yes") if taken else ternary.group("no")
        raise UnresolvedError(
            f"{expression!r} has no local value. Add it to the context rather than "
            f"letting it substitute to nothing -- a blank here silently changes the command."
        )

    return re.sub(r"\$\{\{([^}]*)\}\}", _one, text)


def _evaluate_if(condition: str, context: dict[str, str]) -> bool:
    """Evaluate the small condition grammar the workflow actually uses.

    Supports ``always()``, ``!cancelled()``, ``success()``, ``failure()``,
    ``<ctx> == 'value'``, ``!=``, and ``&&`` / ``||`` between those. Anything
    else raises, because a parity run that assumes a step would have run is
    reporting on a job it did not reproduce.
    """
    expression = condition.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()

    def _atom(text: str) -> bool:
        text = text.strip()
        if text in {"always()", "!cancelled()", "success()"}:
            # Locally every prior step has passed or we would have stopped.
            return True
        if text in {"failure()", "cancelled()"}:
            return False
        match = re.fullmatch(r"([A-Za-z_][\w.\-]*)\s*(==|!=)\s*'([^']*)'", text)
        if match is None:
            raise UnresolvedError(f"cannot evaluate condition {text!r}")
        left, operator, right = match.groups()
        if left not in context:
            raise UnresolvedError(f"condition {text!r} reads {left!r}, which has no local value")
        return (context[left] == right) if operator == "==" else (context[left] != right)

    for operator, combine in (("||", any), ("&&", all)):
        if operator in expression:
            return combine(_atom(part) for part in expression.split(operator))
    return _atom(expression)


def _steps_for(job: dict[str, Any], context: dict[str, str]) -> list[dict[str, Any]]:
    """Resolve a job's steps into commands, actions, and skips."""
    resolved: list[dict[str, Any]] = []
    for step in job.get("steps", []):
        name = step.get("name") or step.get("uses") or (str(step.get("run", "")).splitlines() or [""])[0]
        if "if" in step:
            try:
                if not _evaluate_if(str(step["if"]), context):
                    resolved.append({"kind": "skipped", "name": name, "why": f"if: {step['if']}"})
                    continue
            except UnresolvedError as exc:
                resolved.append({"kind": "unevaluated", "name": name, "why": str(exc)})
                continue
        if "uses" in step:
            resolved.append({"kind": "action", "name": name, "why": step["uses"]})
            continue
        if "run" not in step:  # pragma: no cover - a step is one or the other
            continue
        env = {**{str(k): str(v) for k, v in (job.get("env") or {}).items()}}
        env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})
        try:
            command = _substitute(str(step["run"]), context)
            step_env = {key: _substitute(value, context) for key, value in env.items()}
        except UnresolvedError as exc:
            # One unresolvable step must not cost the other twelve. Report it
            # as not reproduced and carry on -- the run is then honestly
            # described as partial rather than silently narrowed.
            resolved.append({"kind": "unevaluated", "name": name, "why": str(exc)})
            continue
        resolved.append({"kind": "run", "name": name, "command": command, "env": step_env})
    return resolved


def _git(*args: str) -> str | None:
    """A git value, or None if git cannot answer -- never a fabricated one."""
    result = subprocess.run(["git", *args], cwd=_ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() or None if result.returncode == 0 else None


def _context_for(job: dict[str, Any], selections: dict[str, str], temp_dir: Path) -> dict[str, str]:
    context = dict(_STATIC_CONTEXT)
    context["runner.temp"] = str(temp_dir)
    # The local analogues of a push event. `event.before` drives changed-only
    # gates, so HEAD~1 gives them the same shape of answer -- "what this commit
    # changed" -- rather than the empty string, which several of them read as
    # "everything" and one as "nothing".
    local = {
        "github.ref_name": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "github.event.before": _git("rev-parse", "HEAD~1"),
    }
    context.update({key: value for key, value in local.items() if value is not None})
    for key, values in _matrix_of(job).items():
        chosen = selections.get(key)
        if chosen is None:
            chosen = str(values[0])
        if chosen not in {str(value) for value in values}:
            raise SystemExit(f"matrix.{key}={chosen!r} is not one of {[str(v) for v in values]}")
        context[f"matrix.{key}"] = chosen
    return context


def _print_listing(jobs: dict[str, Any]) -> int:
    for name, job in sorted(jobs.items()):
        dimensions = _matrix_of(job)
        axes = "  ".join(f"{key}={','.join(str(v) for v in values)}" for key, values in dimensions.items())
        runs = sum(1 for step in job.get("steps", []) if "run" in step)
        print(f"{name:<32} {runs:>2} command(s)  {axes}")
    return 0


def _run(job_name: str, args: argparse.Namespace) -> int:
    jobs = _load_jobs()
    if job_name not in jobs:
        raise SystemExit(f"no job {job_name!r} in {_WORKFLOW.relative_to(_ROOT)}; try --list")
    job = jobs[job_name]

    selections = dict(pair.split("=", 1) for pair in args.matrix)
    if args.python is not None:
        selections["python-version"] = args.python
    temp_dir = _ROOT / ".ci-parity-tmp"
    temp_dir.mkdir(exist_ok=True)

    context = _context_for(job, selections, temp_dir)
    chosen = "  ".join(f"{key.split('.', 1)[1]}={value}" for key, value in context.items() if key.startswith("matrix."))
    print(f"== {job_name} =={('  ' + chosen) if chosen else ''}", flush=True)

    steps = _steps_for(job, context)
    base_env = dict(os.environ)
    if not args.no_ci_env:
        # Parity includes the environment: thresholds, hypothesis profile and
        # the goldens delegation all read CI.
        base_env["CI"] = "true"

    for step in steps:
        if step["kind"] == "action":
            print(f"  -- not reproducible locally: {step['name']}  ({step['why']})", file=sys.stderr)
            continue
        if step["kind"] == "skipped":
            print(f"  -- skipped by its condition: {step['name']}  ({step['why']})", file=sys.stderr)
            continue
        if step["kind"] == "unevaluated":
            print(f"  !! NOT REPRODUCED: {step['name']}  ({step['why']})", file=sys.stderr)
            continue
        command = step["command"]
        if not args.with_setup and any(marker in command for marker in _SETUP_MARKERS):
            print(
                f"  -- provisioning skipped (--with-setup to run): {command.strip().splitlines()[0]}", file=sys.stderr
            )
            continue
        print(f"\n$ {command.strip()}", flush=True)
        if args.print_only:
            continue
        completed = subprocess.run(  # noqa: S602 - the workflow's own shell commands, by design
            command, shell=True, cwd=_ROOT, env={**base_env, **step["env"]}, check=False
        )
        if completed.returncode != 0:
            print(f"\nFAILED: {step['name']} (exit {completed.returncode})", file=sys.stderr)
            return completed.returncode
    print(f"\n{job_name}: every reproducible step passed", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job", nargs="?", help="job name from .github/workflows/ci.yml")
    parser.add_argument("--list", action="store_true", help="list jobs and their matrix axes")
    parser.add_argument("--python", help="shorthand for --matrix python-version=<v>")
    parser.add_argument("--matrix", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--print", dest="print_only", action="store_true", help="show the commands, run nothing")
    parser.add_argument(
        "--with-setup",
        action="store_true",
        help=f"also run provisioning steps {list(_SETUP_MARKERS)} -- these target a throwaway runner "
        "and `uv sync --frozen --group dev` will uninstall workspace packages from your env",
    )
    parser.add_argument("--no-ci-env", action="store_true", help="do not set CI=true (CI sets it; thresholds read it)")
    args = parser.parse_args(argv)

    if args.list or args.job is None:
        return _print_listing(_load_jobs())
    return _run(args.job, args)


if __name__ == "__main__":
    raise SystemExit(main())
