#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the matrix itself: every client against every server.

The processes are stubbed here — starting four real servers is what the
end-to-end test does — so what is under test is the orchestration: which
cells run, what makes one fail, and how a cell that disagrees with the rest
is caught even when its own expectations all held.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harness.drivers import DriverError, DriverSpec
from harness.matrix import run_matrix
from harness.scenario import load_scenario

_SCENARIO = {
    "id": "001_example",
    "title": "An example",
    "steps": [{"id": "health", "action": "health"}],
    "expect": [{"step": "health", "path": "status", "equals": 200, "why": "because"}],
}


@pytest.fixture
def scenario(tmp_path: Path) -> Any:
    path = tmp_path / "001_example.json"
    path.write_text(json.dumps(_SCENARIO))
    return load_scenario(path)


def _spec(language: str) -> DriverSpec:
    return DriverSpec(language=language, command=("true",))


def _result(language: str, status: int = 200, extra: Any = None) -> dict[str, Any]:
    fields: dict[str, Any] = {"status": status, "ok": status < 400, "body": {"status": "ok"}}
    if extra is not None:
        fields["body"] = extra
    return {
        "scenario_id": "001_example",
        "language": language,
        "role": "client",
        "status": "completed",
        "steps": [{"id": "health", "fields": fields}],
    }


class _Runner:
    """A stand-in for the process mechanics, answering whatever a test says."""

    def __init__(
        self,
        answers: dict[tuple[str, str], Any],
        *,
        server_capabilities: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.answers = answers
        self.started: list[str] = []
        self.ran: list[tuple[str, str]] = []
        self.server_capabilities = server_capabilities or {}

    def start_server(self, spec: DriverSpec, **_: Any) -> Any:
        self.started.append(spec.language)
        return _Server(spec.language, self.server_capabilities.get(spec.language, ("status.observed",)))

    def run_client(self, spec: DriverSpec, *, server_language: str, **_: Any) -> dict[str, Any]:
        self.ran.append((server_language, spec.language))
        answer = self.answers[(server_language, spec.language)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Server:
    def __init__(self, language: str, capabilities: tuple[str, ...] = ("status.observed",)) -> None:
        self.language = language
        self.base_url = f"http://127.0.0.1:0/{language}"
        self.token = "t"
        self.capabilities = capabilities

    def __enter__(self) -> _Server:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class TestEveryCell:
    def test_runs_every_client_against_every_server(self, scenario: Any) -> None:
        languages = ("python", "go", "typescript")
        runner = _Runner({(s, c): _result(c) for s in languages for c in languages})
        report = run_matrix(
            [scenario],
            servers=[_spec(name) for name in languages],
            clients=[_spec(name) for name in languages],
            runner=runner,
        )
        assert len(report.cells) == 9
        assert report.ok
        assert {(cell.server, cell.client) for cell in report.cells} == {(s, c) for s in languages for c in languages}

    def test_starts_each_server_once_per_scenario_not_once_per_client(self, scenario: Any) -> None:
        # Standing a server up is the expensive part; four clients share one.
        runner = _Runner({("python", client): _result(client) for client in ("python", "go")})
        run_matrix(
            [scenario],
            servers=[_spec("python")],
            clients=[_spec("python"), _spec("go")],
            runner=runner,
        )
        assert runner.started == ["python"]


class TestCapabilities:
    @staticmethod
    def _fanout_scenario(tmp_path: Path) -> Any:
        raw = {**_SCENARIO, "requires": ["fanout.rest.strict"]}
        path = tmp_path / "001_example.json"
        path.write_text(json.dumps(raw))
        return load_scenario(path)

    def test_fanout_requires_the_announced_server_capability(self, tmp_path: Path) -> None:
        scenario = self._fanout_scenario(tmp_path)
        runner = _Runner(
            {("typescript", "python"): _result("python")},
            server_capabilities={"typescript": ("status.observed",)},
        )

        report = run_matrix(
            [scenario],
            servers=[_spec("typescript")],
            clients=[_spec("python")],
            runner=runner,
        )

        (cell,) = report.cells
        assert cell.status == "unsupported"
        assert "typescript server" in str(cell.detail)
        assert "fanout.rest.strict" in str(cell.detail)
        assert runner.ran == []

    def test_fanout_requires_the_selected_client_capability(self, tmp_path: Path) -> None:
        scenario = self._fanout_scenario(tmp_path)
        completed_with_fanout = _result("typescript")
        completed_with_fanout["capabilities"] = ["status.observed", "fanout.rest.strict"]
        runner = _Runner(
            {("python", "typescript"): completed_with_fanout},
            server_capabilities={"python": ("status.observed", "fanout.rest.strict")},
        )
        client = SimpleNamespace(
            language="typescript",
            command=("true",),
            cwd=None,
            env={},
            client_capabilities=("status.observed",),
        )

        report = run_matrix(
            [scenario],
            servers=[_spec("python")],
            clients=[client],
            runner=runner,
        )

        (cell,) = report.cells
        assert cell.status == "unsupported"
        assert "typescript client" in str(cell.detail)
        assert "fanout.rest.strict" in str(cell.detail)
        assert runner.ran == []

    def test_client_result_capabilities_are_still_validated_after_preflight(self, tmp_path: Path) -> None:
        scenario = self._fanout_scenario(tmp_path)
        completed_without_fanout = _result("typescript")
        completed_without_fanout["capabilities"] = ["status.observed"]
        runner = _Runner(
            {("python", "typescript"): completed_without_fanout},
            server_capabilities={"python": ("status.observed", "fanout.rest.strict")},
        )
        client = SimpleNamespace(
            language="typescript",
            command=("true",),
            cwd=None,
            env={},
            client_capabilities=("status.observed", "fanout.rest.strict"),
        )

        report = run_matrix(
            [scenario],
            servers=[_spec("python")],
            clients=[client],
            runner=runner,
        )

        (cell,) = report.cells
        assert cell.status == "unsupported"
        assert "typescript client" in str(cell.detail)
        assert "fanout.rest.strict" in str(cell.detail)
        assert runner.ran == [("python", "typescript")]


class TestVerdicts:
    def test_a_cell_whose_expectation_fails_is_a_failure(self, scenario: Any) -> None:
        runner = _Runner({("python", "python"): _result("python", status=500)})
        report = run_matrix([scenario], servers=[_spec("python")], clients=[_spec("python")], runner=runner)
        (cell,) = report.cells
        assert cell.status == "fail"
        assert cell.failures[0].actual == 500
        assert not report.ok

    def test_a_driver_that_broke_is_an_error_cell_not_a_crash(self, scenario: Any) -> None:
        # One language's driver falling over must not cost the other fifteen
        # cells their run.
        runner = _Runner(
            {
                ("python", "python"): DriverError("go away"),
                ("python", "go"): _result("go"),
            }
        )
        report = run_matrix(
            [scenario],
            servers=[_spec("python")],
            clients=[_spec("python"), _spec("go")],
            runner=runner,
        )
        broken = next(cell for cell in report.cells if cell.client == "python")
        assert broken.status == "error"
        assert "go away" in str(broken.detail)
        assert next(cell for cell in report.cells if cell.client == "go").status == "pass"

    def test_an_unsupported_cell_is_neither_pass_nor_fail(self, scenario: Any) -> None:
        unsupported = {
            "scenario_id": "001_example",
            "language": "csharp",
            "role": "client",
            "status": "unsupported",
            "steps": [],
            "error": "no graphical console here",
        }
        runner = _Runner({("python", "csharp"): unsupported})
        report = run_matrix([scenario], servers=[_spec("python")], clients=[_spec("csharp")], runner=runner)
        (cell,) = report.cells
        assert cell.status == "unsupported"
        # It does not fail the run, but it is not silence either.
        assert report.ok
        assert "graphical" in str(cell.detail)

    def test_a_driver_reporting_its_own_error_is_an_error_cell(self, scenario: Any) -> None:
        errored = {
            "scenario_id": "001_example",
            "language": "go",
            "role": "client",
            "status": "error",
            "steps": [],
            "error": "could not resolve host",
        }
        runner = _Runner({("python", "go"): errored})
        report = run_matrix([scenario], servers=[_spec("python")], clients=[_spec("go")], runner=runner)
        assert report.cells[0].status == "error"
        assert not report.ok


class TestAgreement:
    def test_a_cell_that_disagrees_with_the_reference_fails_even_when_its_own_checks_hold(self, scenario: Any) -> None:
        # This is the whole point of the matrix. Both cells satisfy every
        # expectation the scenario wrote down; they still saw different
        # servers, and that is a divergence the scenario did not think to ask
        # about.
        runner = _Runner(
            {
                ("python", "python"): _result("python", extra={"status": "ok", "service": "uterm-server"}),
                ("go", "python"): _result("python", extra={"status": "ok"}),
            }
        )
        report = run_matrix(
            [scenario],
            servers=[_spec("python"), _spec("go")],
            clients=[_spec("python")],
            runner=runner,
        )
        divergent = next(cell for cell in report.cells if cell.server == "go")
        assert divergent.status == "fail"
        assert divergent.differences[0].path == "health.body.service"
        assert not report.ok

    def test_agreement_is_measured_against_the_reference_pair(self, scenario: Any) -> None:
        runner = _Runner(
            {
                ("python", "python"): _result("python"),
                ("python", "go"): _result("go"),
            }
        )
        report = run_matrix(
            [scenario],
            servers=[_spec("python")],
            clients=[_spec("python"), _spec("go")],
            runner=runner,
        )
        assert report.ok
        assert all(cell.differences == () for cell in report.cells)

    def test_a_declared_volatile_field_is_allowed_to_differ(self, tmp_path: Path) -> None:
        raw = {
            **_SCENARIO,
            "steps": [{"id": "health", "action": "health", "volatile": ["body.uptime_s"]}],
        }
        path = tmp_path / "001_example.json"
        path.write_text(json.dumps(raw))
        scenario = load_scenario(path)
        runner = _Runner(
            {
                ("python", "python"): _result("python", extra={"uptime_s": 1.5}),
                ("go", "python"): _result("python", extra={"uptime_s": 900.25}),
            }
        )
        report = run_matrix(
            [scenario],
            servers=[_spec("python"), _spec("go")],
            clients=[_spec("python")],
            runner=runner,
        )
        assert report.ok

    def test_the_reference_cell_missing_leaves_the_others_uncompared_but_checked(self, scenario: Any) -> None:
        # With no python-on-python cell there is nothing to be the reference,
        # so the first completed cell becomes it — the run still means
        # something rather than silently skipping the comparison.
        runner = _Runner({("go", "go"): _result("go")})
        report = run_matrix([scenario], servers=[_spec("go")], clients=[_spec("go")], runner=runner)
        assert report.ok
        assert report.cells[0].status == "pass"


class TestServerFailure:
    def test_a_server_that_will_not_start_fails_its_whole_row(self, scenario: Any) -> None:
        class _Broken(_Runner):
            def start_server(self, spec: DriverSpec, **_: Any) -> Any:
                raise DriverError(f"{spec.language} would not bind")

        report = run_matrix(
            [scenario],
            servers=[_spec("csharp")],
            clients=[_spec("python"), _spec("go")],
            runner=_Broken({}),
        )
        assert [cell.status for cell in report.cells] == ["error", "error"]
        assert all("would not bind" in str(cell.detail) for cell in report.cells)
        assert not report.ok


class TestWhatEachCheckSees:
    """Masking is for the comparison, not for the expectations."""

    def test_an_expectation_sees_the_real_value_of_a_volatile_field(self, tmp_path: Path) -> None:
        # A lease expiry is volatile — no two runs agree on it — but a
        # scenario still has to be able to say it is a number rather than a
        # formatted time. Masking before checking made that impossible: every
        # volatile field looked like the string "<volatile>".
        raw = {
            **_SCENARIO,
            "steps": [{"id": "health", "action": "health", "volatile": ["body.expires_at"]}],
            "expect": [{"step": "health", "path": "body.expires_at", "type": "number", "why": "because"}],
        }
        path = tmp_path / "001_example.json"
        path.write_text(json.dumps(raw))
        scenario = load_scenario(path)
        runner = _Runner({("python", "python"): _result("python", extra={"expires_at": 1785331723.5})})
        report = run_matrix([scenario], servers=[_spec("python")], clients=[_spec("python")], runner=runner)
        assert report.cells[0].status == "pass", report.cells[0].failures

    def test_the_comparison_still_ignores_it(self, tmp_path: Path) -> None:
        # And the other half must keep holding: two cells that saw different
        # expiries have not seen different servers.
        raw = {
            **_SCENARIO,
            "steps": [{"id": "health", "action": "health", "volatile": ["body.expires_at"]}],
            "expect": [{"step": "health", "path": "body.expires_at", "type": "number", "why": "because"}],
        }
        path = tmp_path / "001_example.json"
        path.write_text(json.dumps(raw))
        scenario = load_scenario(path)
        runner = _Runner(
            {
                ("python", "python"): _result("python", extra={"expires_at": 1.0}),
                ("go", "python"): _result("python", extra={"expires_at": 99999.0}),
            }
        )
        report = run_matrix(
            [scenario], servers=[_spec("python"), _spec("go")], clients=[_spec("python")], runner=runner
        )
        assert report.ok


class TestIsolation:
    """A scenario that changes the server cannot share one."""

    def _mutating(self, tmp_path: Path, mutates: bool) -> Any:
        raw = {**_SCENARIO, "mutates": mutates}
        path = tmp_path / "001_example.json"
        path.write_text(json.dumps(raw))
        return load_scenario(path)

    def test_a_read_only_scenario_shares_one_server_across_its_clients(self, tmp_path: Path) -> None:
        # Standing a server up is the expensive part of a cell. Nothing a
        # read-only scenario does can be seen by the next client, so it pays
        # for one server and uses it four times.
        runner = _Runner({("python", client): _result(client) for client in ("python", "go")})
        run_matrix(
            [self._mutating(tmp_path, False)],
            servers=[_spec("python")],
            clients=[_spec("python"), _spec("go")],
            runner=runner,
        )
        assert runner.started == ["python"]

    def test_a_mutating_scenario_gets_a_fresh_server_for_each_client(self, tmp_path: Path) -> None:
        # A scenario that puts a session into hijack mode and takes its lease
        # leaves the next client somewhere the scenario never described. The
        # second client's first step would then be answered by a server the
        # first client had already changed.
        runner = _Runner({("python", client): _result(client) for client in ("python", "go")})
        run_matrix(
            [self._mutating(tmp_path, True)],
            servers=[_spec("python")],
            clients=[_spec("python"), _spec("go")],
            runner=runner,
        )
        assert runner.started == ["python", "python"]

    def test_a_server_that_will_not_start_still_fails_only_its_own_cell(self, tmp_path: Path) -> None:
        class _BrokenOnce(_Runner):
            def start_server(self, spec: DriverSpec, **kwargs: Any) -> Any:
                self.started.append(spec.language)
                if len(self.started) == 1:
                    raise DriverError("first start failed")
                return _Server(spec.language)

        runner = _BrokenOnce({("python", "go"): _result("go")})
        report = run_matrix(
            [self._mutating(tmp_path, True)],
            servers=[_spec("python")],
            clients=[_spec("python"), _spec("go")],
            runner=runner,
        )
        assert [cell.status for cell in report.cells] == ["error", "pass"]
