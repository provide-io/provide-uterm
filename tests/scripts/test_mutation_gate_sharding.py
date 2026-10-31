#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The sharded Python mutation gate: target list, verdict, progress, and wiring.

ci.yml's ``mutation-gate`` used to be one job that ran every changed perimeter
file end to end. PR #165 touched enough of the perimeter to spend the job's
whole 90 minutes and was cancelled with no verdict, and the log said nothing
after the gate's first line because mutmut's progress never reached it. The
gate is now three jobs: ``mutation-targets`` lists the files
(``ci/prepare_mutation_args.sh``), ``mutation-shards`` runs one job per file
(``.github/workflows/mutation-shards.yml``, shared with ``mutation-full.yml``),
and ``mutation-gate`` turns their results into one verdict
(``ci/mutation_gate_verdict.sh``).

Each piece fails open in its own way if it is wrong: an empty list where there
should be targets, a skipped or cancelled shard read as a pass, a gate whose
output is held back until it exits. These pin each of those, plus the parts of
the workflow wiring that the pieces rely on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import run_mutation_gate

_PREPARE = _ROOT / "ci" / "prepare_mutation_args.sh"
_VERDICT = _ROOT / "ci" / "mutation_gate_verdict.sh"
_WORKFLOWS = _ROOT / ".github" / "workflows"


# ---------------------------------------------------------------------------
# run_mutation_gate.py --emit-targets
# ---------------------------------------------------------------------------


def _emit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sources: list[str],
    support: list[str],
) -> tuple[int, str]:
    ran: list[object] = []
    monkeypatch.setattr(run_mutation_gate, "_changed_python_paths", lambda *_a, **_k: list(sources))
    monkeypatch.setattr(run_mutation_gate, "_changed_paths", lambda *_a, **_k: list(support))
    monkeypatch.setattr(run_mutation_gate, "_changed_mutation_support_paths", lambda _p, *_a, **_k: list(support))
    monkeypatch.setattr(run_mutation_gate, "run_mutation_gate", lambda *a, **k: ran.append((a, k)))
    monkeypatch.setattr(
        sys, "argv", ["run_mutation_gate.py", "--changed-only", "--emit-targets", "--base-ref", "origin/main"]
    )
    rc = run_mutation_gate.main()
    assert ran == [], "--emit-targets must never start mutmut"
    return rc, capsys.readouterr().out


def _targets_line(out: str) -> list[str]:
    lines = [line for line in out.splitlines() if line.startswith(run_mutation_gate.TARGETS_LINE_PREFIX)]
    assert len(lines) == 1, out
    return list(json.loads(lines[0].removeprefix(run_mutation_gate.TARGETS_LINE_PREFIX)))


class TestEmitTargets:
    def test_changed_sources_are_listed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sources = ["src/provide/uterm/auth.py", "src/provide/uterm/io.py"]
        rc, out = _emit(monkeypatch, capsys, sources, [])
        assert rc == 0
        assert _targets_line(out) == sources
        assert run_mutation_gate.FULL_PERIMETER_REQUIRED_MARKER not in out

    def test_nothing_changed_is_an_empty_list_not_a_missing_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _emit(monkeypatch, capsys, [], [])
        assert rc == 0
        assert _targets_line(out) == []

    def test_a_support_change_prints_the_marker(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _emit(monkeypatch, capsys, ["src/provide/uterm/auth.py"], ["mutation_equivalents.toml"])
        assert rc == 0
        assert run_mutation_gate.FULL_PERIMETER_REQUIRED_MARKER in out.splitlines()

    def test_its_own_output_is_line_buffered(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # In CI stdout is a pipe, which Python block-buffers: the gate's own
        # lines then land after mutmut's whole run, or never, if the job is
        # killed first.
        calls: list[dict[str, object]] = []

        class _Stdout:
            def reconfigure(self, **kwargs: object) -> None:
                calls.append(kwargs)

            def write(self, text: str) -> int:
                return len(text)

            def flush(self) -> None:
                pass

        monkeypatch.setattr(sys, "stdout", _Stdout())
        _emit(monkeypatch, capsys, [], [])
        assert calls == [{"line_buffering": True}]

    def test_it_refuses_without_changed_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["run_mutation_gate.py", "--emit-targets"])
        with pytest.raises(SystemExit) as raised:
            run_mutation_gate.main()
        assert raised.value.code == 2


# ---------------------------------------------------------------------------
# ci/prepare_mutation_args.sh
# ---------------------------------------------------------------------------

_FULL = ["src/provide/uterm/a.py", "src/provide/uterm/b.py"]

# Stands in for python3: the script's only two calls are the gate in
# --emit-targets mode and the full-perimeter generator, so the fake answers
# those from the environment and records how it was called.
_FAKE_PYTHON = """#!/usr/bin/env bash
echo "$*" >> "${FAKE_LOG}"
case "$1" in
  ci/mutation_full_matrix.py) printf '%s\\n' "${FAKE_FULL}"; exit "${FAKE_FULL_STATUS:-0}" ;;
  scripts/run_mutation_gate.py) printf '%b' "${FAKE_GATE_OUT}"; exit "${FAKE_GATE_STATUS:-0}" ;;
  *) echo "unexpected python3 call: $*" >&2; exit 99 ;;
esac
"""


def _prepare(tmp_path: Path, event: str, gate_out: str = "", gate_status: int = 0, **env: str) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    fake.write_text(_FAKE_PYTHON, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_FULL": json.dumps(_FULL, separators=(",", ":")),
        "FAKE_GATE_OUT": gate_out,
        "FAKE_GATE_STATUS": str(gate_status),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_OUTPUT": str(output),
        **env,
    }
    result = subprocess.run(
        ["bash", str(_PREPARE)], cwd=_ROOT, env=full_env, capture_output=True, text=True, check=False
    )
    lines = output.read_text(encoding="utf-8").splitlines()
    paths = [line.removeprefix("paths=") for line in lines if line.startswith("paths=")]
    return {
        "rc": result.returncode,
        "stderr": result.stderr,
        "paths": json.loads(paths[0]) if paths else None,
        "calls": log.read_text(encoding="utf-8").splitlines(),
    }


class TestPrepareMutationArgs:
    def test_a_pr_lists_what_it_changed_against_its_base(self, tmp_path: Path) -> None:
        got = _prepare(
            tmp_path, "pull_request", 'MUTATION_TARGETS=["src/provide/uterm/auth.py"]\\n', GITHUB_BASE_REF="main"
        )
        assert got["rc"] == 0, got["stderr"]
        assert got["paths"] == ["src/provide/uterm/auth.py"]
        assert got["calls"] == ["scripts/run_mutation_gate.py --changed-only --emit-targets --base-ref origin/main"]

    def test_a_pr_that_changed_no_target_gets_an_empty_list(self, tmp_path: Path) -> None:
        got = _prepare(tmp_path, "pull_request", "MUTATION_TARGETS=[]\\n", GITHUB_BASE_REF="main")
        assert got["rc"] == 0, got["stderr"]
        assert got["paths"] == []

    def test_a_support_change_runs_the_whole_perimeter_on_the_pr(self, tmp_path: Path) -> None:
        # The PR answers it now; it used to pass and leave it to a dispatch
        # after merge.
        out = 'mutation gate full-perimeter trigger: ...\\nMUTATION_FULL_PERIMETER_REQUIRED\\nMUTATION_TARGETS=["src/provide/uterm/a.py"]\\n'
        got = _prepare(tmp_path, "pull_request", out, GITHUB_BASE_REF="main")
        assert got["rc"] == 0, got["stderr"]
        assert got["paths"] == _FULL

    def test_a_push_diffs_against_its_before_sha(self, tmp_path: Path) -> None:
        got = _prepare(tmp_path, "push", "MUTATION_TARGETS=[]\\n", GITHUB_EVENT_BEFORE="abc123")
        assert got["rc"] == 0, got["stderr"]
        assert got["calls"] == ["scripts/run_mutation_gate.py --changed-only --emit-targets --base-ref abc123"]

    @pytest.mark.parametrize("before", ["", "0" * 40])
    def test_a_push_with_no_base_runs_the_whole_perimeter(self, tmp_path: Path, before: str) -> None:
        got = _prepare(tmp_path, "push", GITHUB_EVENT_BEFORE=before)
        assert got["rc"] == 0, got["stderr"]
        assert got["paths"] == _FULL
        assert got["calls"] == ["ci/mutation_full_matrix.py"]

    def test_a_scheduled_run_diffs_against_the_previous_commit(self, tmp_path: Path) -> None:
        got = _prepare(tmp_path, "schedule", "MUTATION_TARGETS=[]\\n")
        assert got["rc"] == 0, got["stderr"]
        assert got["calls"] == ["scripts/run_mutation_gate.py --changed-only --emit-targets --base-ref HEAD~1"]

    def test_a_gate_that_crashes_fails_the_step(self, tmp_path: Path) -> None:
        # The failure that matters: a crash leaving "[]" behind would read as
        # "nothing to mutate" and pass.
        got = _prepare(tmp_path, "pull_request", "Traceback ...\\n", gate_status=1, GITHUB_BASE_REF="main")
        assert got["rc"] != 0
        assert got["paths"] is None

    def test_a_gate_that_prints_no_list_fails_the_step(self, tmp_path: Path) -> None:
        got = _prepare(tmp_path, "pull_request", "something else\\n", GITHUB_BASE_REF="main")
        assert got["rc"] != 0
        assert got["paths"] is None
        assert "printed no MUTATION_TARGETS= line" in got["stderr"]

    def test_a_gate_that_cannot_diff_fails_the_step(self, tmp_path: Path) -> None:
        # What run_mutation_gate.py prints for an unfetchable base: a FAIL line
        # and no array, with a non-zero exit.
        out = "mutation gate: FAIL -- cannot tell what changed against deadbeef: ...\\n"
        got = _prepare(tmp_path, "push", out, gate_status=1, GITHUB_EVENT_BEFORE="deadbeef")
        assert got["rc"] != 0
        assert got["paths"] is None

    _MARKER_OUT = "MUTATION_FULL_PERIMETER_REQUIRED\\nMUTATION_TARGETS=[]\\n"

    @pytest.mark.parametrize(
        ("event", "gate_out", "env"),
        [
            ("pull_request", _MARKER_OUT, {"GITHUB_BASE_REF": "main"}),
            ("push", "", {"GITHUB_EVENT_BEFORE": ""}),
        ],
        ids=["support-change", "push-without-base"],
    )
    def test_a_failing_perimeter_generator_fails_the_step(
        self, tmp_path: Path, event: str, gate_out: str, env: dict[str, str]
    ) -> None:
        got = _prepare(tmp_path, event, gate_out, FAKE_FULL_STATUS="1", **env)
        assert got["rc"] != 0
        assert got["paths"] is None

    @pytest.mark.parametrize("full", ["[]", ""])
    @pytest.mark.parametrize(
        ("event", "gate_out", "env"),
        [
            ("pull_request", _MARKER_OUT, {"GITHUB_BASE_REF": "main"}),
            ("push", "", {"GITHUB_EVENT_BEFORE": ""}),
        ],
        ids=["support-change", "push-without-base"],
    )
    def test_an_empty_perimeter_fails_rather_than_skipping_every_shard(
        self, tmp_path: Path, event: str, gate_out: str, env: dict[str, str], full: str
    ) -> None:
        # "[]" from the generator would skip every shard and pass the gate.
        got = _prepare(tmp_path, event, gate_out, FAKE_FULL=full, **env)
        assert got["rc"] != 0
        assert got["paths"] is None
        assert "empty perimeter" in got["stderr"]


# ---------------------------------------------------------------------------
# ci/mutation_gate_verdict.sh
# ---------------------------------------------------------------------------


def _verdict(targets: str, paths: str, shards: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TARGETS_RESULT": targets, "TARGET_PATHS": paths, "SHARDS_RESULT": shards}
    return subprocess.run(["bash", str(_VERDICT)], env=env, capture_output=True, text=True, check=False)


class TestMutationGateVerdict:
    def test_every_shard_passing_passes(self) -> None:
        assert _verdict("success", '["a.py"]', "success").returncode == 0

    def test_no_targets_passes_with_the_shards_skipped(self) -> None:
        assert _verdict("success", "[]", "skipped").returncode == 0

    @pytest.mark.parametrize("shards", ["failure", "cancelled", "skipped", ""])
    def test_anything_short_of_every_shard_passing_fails(self, shards: str) -> None:
        # Cancelled is a timeout or a lost runner, and skipped never ran:
        # neither produced a verdict, so neither may pass as one.
        result = _verdict("success", '["a.py"]', shards)
        assert result.returncode == 1
        assert "FAIL" in result.stdout

    @pytest.mark.parametrize("targets", ["failure", "cancelled", ""])
    def test_an_uncomputed_target_list_fails_even_if_nothing_ran(self, targets: str) -> None:
        assert _verdict(targets, "[]", "skipped").returncode == 1

    def test_a_missing_list_is_not_read_as_empty(self) -> None:
        assert _verdict("success", "", "skipped").returncode == 1


# ---------------------------------------------------------------------------
# Workflow wiring
# ---------------------------------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return dict(data)


class TestWorkflowWiring:
    def test_the_required_check_keeps_its_name_and_reads_both_jobs(self) -> None:
        job = _workflow("ci.yml")["jobs"]["mutation-gate"]
        assert sorted(job["needs"]) == ["mutation-shards", "mutation-targets"]
        # Without always() the verdict job is skipped exactly when a shard
        # fails, and a skipped check blocks nothing.
        assert job["if"] == "always()"
        assert any(step.get("run") == "ci/mutation_gate_verdict.sh" for step in job["steps"])

    def test_both_callers_share_the_one_shard_definition(self) -> None:
        ci = _workflow("ci.yml")["jobs"]["mutation-shards"]
        full = _workflow("mutation-full.yml")["jobs"]["mutation-gate-full"]
        assert ci["uses"] == full["uses"] == "./.github/workflows/mutation-shards.yml"
        # A matrix cannot be empty, so an empty list has to skip the call.
        assert ci["if"] == "needs.mutation-targets.outputs.paths != '[]'"

    def test_no_python_mutation_job_can_write_to_actions(self) -> None:
        # The only use of actions: write was dispatching mutation-full.yml,
        # and nothing dispatches it any more.
        jobs = _workflow("ci.yml")["jobs"]
        for name in ("mutation-targets", "mutation-shards", "mutation-gate"):
            assert "write" not in (jobs[name].get("permissions") or {}).values(), name

    def test_the_shard_workflow_has_no_concurrency_group(self) -> None:
        # github.workflow names the CALLER inside a called workflow, so a group
        # keyed on it would collide with the caller's own and cancel the run.
        assert "concurrency" not in _workflow("mutation-shards.yml")

    def test_the_gate_step_keeps_the_name_the_failure_reports_read(self) -> None:
        # ci/report_mutation_full_failure.sh tells drift from a setup fault by
        # whether the failing step's name starts with "Mutation gate".
        steps = _workflow("mutation-shards.yml")["jobs"]["target"]["steps"]
        gate_steps = [step for step in steps if "run_mutation_gate.py" in str(step.get("run", ""))]
        assert len(gate_steps) == 1
        assert gate_steps[0]["name"].startswith("Mutation gate")
        assert "--paths" in gate_steps[0]["run"]
        assert "--min-mutation-score 100" in gate_steps[0]["run"]

    def test_the_gate_writes_straight_to_the_log(self) -> None:
        # PR #165's log showed nothing for 90 minutes because the old
        # prepare_mutation_args.sh ran the gate inside out="$(...)", which holds
        # every byte until the gate exits -- and a job cancelled at its timeout
        # never exits. The shard step runs the gate directly, unbuffered.
        steps = _workflow("mutation-shards.yml")["jobs"]["target"]["steps"]
        (gate_step,) = [step for step in steps if "run_mutation_gate.py" in str(step.get("run", ""))]
        assert gate_step["run"].startswith("uv run python scripts/run_mutation_gate.py ")
        assert gate_step["env"]["PYTHONUNBUFFERED"] == "1"

    def test_the_target_path_never_reaches_the_shell_as_code(self) -> None:
        # On a PR the path comes from a changed file name, and git does not
        # quote `$(...)` in names. Through env it is data; interpolated into
        # run: it would be a command.
        steps = _workflow("mutation-shards.yml")["jobs"]["target"]["steps"]
        (gate_step,) = [step for step in steps if "run_mutation_gate.py" in str(step.get("run", ""))]
        assert "matrix." not in gate_step["run"]
        assert gate_step["env"]["TARGET_PATH"] == "${{ matrix.path }}"
        assert '--paths "${TARGET_PATH}"' in gate_step["run"]

    def test_the_target_job_runs_the_script_this_file_tests(self) -> None:
        steps = _workflow("ci.yml")["jobs"]["mutation-targets"]["steps"]
        assert any(step.get("run") == "ci/prepare_mutation_args.sh" for step in steps)
