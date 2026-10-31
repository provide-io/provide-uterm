#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""What makes the Python mutation gate run the perimeter: support files and git failures.

The sharded gate passes an empty target list, so every way of getting the list
wrong is a way of passing a change that was never checked. Two are pinned
here (the per-path classification is in test_mutation_change_scope.py):

* a file that defines what a shard runs or how the verdict is reached must be
  a support file, or editing it alone runs nothing (the shard command left
  that set when it moved into mutation-shards.yml);
* a ``git diff`` that fails must fail the gate, not read as "nothing changed"
  (a force-push leaves a push's ``before`` unfetchable).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import mutation_change_scope, mutation_gate_config, run_mutation_gate

_WORKFLOWS = _ROOT / ".github" / "workflows"
_SCRIPT_RE = re.compile(r"\b((?:ci|scripts)/[\w./-]+\.(?:sh|py))\b")


# ---------------------------------------------------------------------------
# M1: everything on the gate's command path is a support file
# ---------------------------------------------------------------------------


def _gate_entry_points() -> set[str]:
    """Files the CI side of the gate runs directly: run: lines and called workflows."""
    ci = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    shards = yaml.safe_load((_WORKFLOWS / "mutation-shards.yml").read_text(encoding="utf-8"))
    found: set[str] = {".github/workflows/ci.yml"}
    jobs = [ci["jobs"][name] for name in mutation_gate_config.MUTATION_GATE_JOBS]
    jobs += list(shards["jobs"].values())
    for job in jobs:
        uses = str(job.get("uses", ""))
        if uses.startswith("./"):
            found.add(uses.removeprefix("./"))
        for step in job.get("steps", []):
            found.update(_SCRIPT_RE.findall(str(step.get("run", ""))))
    return found


def _reached_from(path: str) -> set[str]:
    """*path* plus every repo script it runs or imports, transitively."""
    seen: set[str] = set()
    pending = [path]
    while pending:
        current = pending.pop()
        if current in seen or not (_ROOT / current).is_file():
            continue
        seen.add(current)
        text = (_ROOT / current).read_text(encoding="utf-8")
        if current.endswith(".py"):
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    name = node.module.removeprefix("scripts.")
                    if (_ROOT / "scripts" / f"{name}.py").is_file():
                        pending.append(f"scripts/{name}.py")
        elif current.endswith(".sh"):
            # Commands only: a comment naming another script does not run it.
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            pending.extend(_SCRIPT_RE.findall(code))
        # A workflow's own text is not walked: its run: lines were collected by
        # _gate_entry_points, and the rest of ci.yml is not the gate.
    return seen


class TestSupportFilesCoverTheCommandPath:
    def test_every_file_the_gate_runs_is_a_support_file(self) -> None:
        reached: set[str] = set()
        for entry in _gate_entry_points():
            reached |= _reached_from(entry)
        missing = sorted(reached - set(mutation_gate_config.MUTATION_SUPPORT_FILES))
        assert not missing, (
            "these define what the gate runs or how it passes, but editing one alone would run "
            f"nothing: add them to MUTATION_SUPPORT_FILES: {missing}"
        )

    def test_the_walk_reaches_the_files_it_exists_for(self) -> None:
        # Guards the test above against passing because it found nothing.
        reached: set[str] = set()
        for entry in _gate_entry_points():
            reached |= _reached_from(entry)
        assert {
            ".github/workflows/mutation-shards.yml",
            "ci/prepare_mutation_args.sh",
            "ci/mutation_full_matrix.py",
            "ci/mutation_gate_verdict.sh",
            "scripts/run_mutation_gate.py",
            "scripts/mutation_gate_config.py",
            "scripts/mutation_change_scope.py",
        } <= reached

    @pytest.mark.parametrize(
        "path",
        [
            p
            for p in mutation_gate_config.MUTATION_SUPPORT_FILES
            if p not in {"pyproject.toml", mutation_gate_config.CI_WORKFLOW}
        ],
    )
    def test_a_change_to_each_one_alone_widens_to_the_perimeter(self, path: str) -> None:
        assert run_mutation_gate._changed_mutation_support_paths([path]) == [path]


_CI = """jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check
  mutation-shards:
    # why the retries are what they are
    uses: ./.github/workflows/mutation-shards.yml
    with:
      retries: 1
  other:
    runs-on: ubuntu-latest
"""


class TestCiWorkflowCountsOnlyForTheGateJobs:
    def _changed(self, monkeypatch: pytest.MonkeyPatch, after: str) -> list[str]:
        monkeypatch.setattr(run_mutation_gate, "_git_show", lambda spec: _CI)
        monkeypatch.setattr(run_mutation_gate, "_read_text_or_none", lambda _path: after)
        return run_mutation_gate._changed_mutation_support_paths([mutation_gate_config.CI_WORKFLOW], "base")

    def test_an_edit_to_a_gate_job_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._changed(monkeypatch, _CI.replace("retries: 1", "retries: 0")) == [".github/workflows/ci.yml"]

    def test_an_edit_to_another_job_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._changed(monkeypatch, _CI.replace("ruff check", "ruff check --fix")) == []

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("jobs:", "env:\n  PYTHONHASHSEED: '0'\njobs:"),
            ("jobs:", "on:\n  push:\njobs:"),
            ("jobs:", "permissions:\n  contents: read\njobs:"),
        ],
        ids=["env", "on", "permissions"],
    )
    def test_an_edit_to_a_workflow_level_key_counts(
        self, monkeypatch: pytest.MonkeyPatch, before: str, after: str
    ) -> None:
        # Workflow-level keys apply to the gate jobs too; an `on:` edit can
        # stop the gate running on pull requests at all.
        assert self._changed(monkeypatch, _CI.replace(before, after, 1)) == [".github/workflows/ci.yml"]

    def test_rewording_a_comment_in_a_gate_job_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._changed(monkeypatch, _CI.replace("why the retries", "the reason the retries")) == []

    def test_an_unreadable_side_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mutation_gate, "_git_show", lambda spec: None)
        monkeypatch.setattr(run_mutation_gate, "_read_text_or_none", lambda _path: _CI)
        assert run_mutation_gate._changed_mutation_support_paths([mutation_gate_config.CI_WORKFLOW], "base") == [
            ".github/workflows/ci.yml"
        ]

    def test_the_real_workflow_has_every_gate_job(self) -> None:
        # A renamed job, or a workflow-level key the parser stopped seeing,
        # would silently drop out of the comparison.
        blocks = mutation_change_scope.gate_blocks(
            (_ROOT / mutation_gate_config.CI_WORKFLOW).read_text(encoding="utf-8"),
            mutation_gate_config.MUTATION_GATE_JOBS,
        )
        assert blocks is not None
        assert set(mutation_gate_config.MUTATION_GATE_JOBS) <= set(blocks)
        assert {"top-level on", "top-level permissions", "top-level concurrency"} <= set(blocks)
        assert "top-level jobs" not in blocks
        assert all(blocks.values())


# ---------------------------------------------------------------------------
# M2: a failed git diff fails the gate
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "a")
    monkeypatch.chdir(tmp_path)
    return tmp_path


_UNREACHABLE = "1234567890abcdef1234567890abcdef12345678"


class TestAFailedDiffFails:
    def test_changed_paths_raises_rather_than_answering_nothing(self, repo: Path) -> None:
        with pytest.raises(run_mutation_gate.ChangedPathsError):
            run_mutation_gate._changed_paths(_UNREACHABLE, False)

    def test_a_good_base_still_diffs(self, repo: Path) -> None:
        (repo / "a.txt").write_text("b\n", encoding="utf-8")
        assert run_mutation_gate._changed_paths("HEAD", False) == ["a.txt"]

    def test_emit_targets_fails_and_prints_no_list(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            sys, "argv", ["run_mutation_gate.py", "--changed-only", "--emit-targets", "--base-ref", _UNREACHABLE]
        )
        assert run_mutation_gate.main() == 1
        out = capsys.readouterr().out
        assert run_mutation_gate.TARGETS_LINE_PREFIX not in out
        assert "cannot tell what changed" in out
