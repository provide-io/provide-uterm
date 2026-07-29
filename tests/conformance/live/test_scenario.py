#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for loading a scenario, and for the scenarios actually committed.

A scenario is the contract sixteen cells are held to, so a malformed one has
to be refused at load rather than quietly producing sixteen agreeing wrong
answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness.scenario import SCENARIO_DIR, load_scenario, load_scenarios

_MINIMAL = {
    "id": "001_example",
    "title": "An example",
    "steps": [{"id": "health", "action": "health"}],
    "expect": [{"step": "health", "path": "status", "equals": 200}],
}


def _write(tmp_path: Path, scenario: dict, name: str = "001_example.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(scenario))
    return path


class TestLoad:
    def test_reads_the_steps_and_expectations(self, tmp_path: Path) -> None:
        scenario = load_scenario(_write(tmp_path, _MINIMAL))
        assert scenario.id == "001_example"
        assert [step.id for step in scenario.steps] == ["health"]
        assert scenario.expectations[0].predicate == "equals"

    def test_defaults_are_the_documented_ones(self, tmp_path: Path) -> None:
        scenario = load_scenario(_write(tmp_path, _MINIMAL))
        assert scenario.timeout_ms == 15000
        assert scenario.auth == "dev_token"
        assert scenario.requires == ()
        assert scenario.steps[0].auth == "token"

    def test_carries_the_volatile_paths_per_step(self, tmp_path: Path) -> None:
        raw = {
            **_MINIMAL,
            "steps": [{"id": "health", "action": "health", "volatile": ["body.uptime_s"]}],
        }
        assert load_scenario(_write(tmp_path, raw)).volatile_by_step == {"health": ("body.uptime_s",)}

    def test_refuses_a_scenario_the_schema_rejects(self, tmp_path: Path) -> None:
        raw = {**_MINIMAL, "steps": [{"id": "health", "action": "teleport"}]}
        with pytest.raises(ValueError, match="does not match"):
            load_scenario(_write(tmp_path, raw))

    def test_refuses_two_steps_with_one_id(self, tmp_path: Path) -> None:
        # Observations are keyed by step id, so a duplicate would silently
        # throw one of the two away in every language at once.
        raw = {**_MINIMAL, "steps": [{"id": "health", "action": "health"}, {"id": "health", "action": "health"}]}
        with pytest.raises(ValueError, match="twice"):
            load_scenario(_write(tmp_path, raw))

    def test_refuses_an_expectation_about_a_step_that_is_not_there(self, tmp_path: Path) -> None:
        raw = {**_MINIMAL, "expect": [{"step": "typo", "path": "status", "equals": 200}]}
        with pytest.raises(ValueError, match="typo"):
            load_scenario(_write(tmp_path, raw))

    def test_refuses_an_id_that_does_not_match_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="file name"):
            load_scenario(_write(tmp_path, _MINIMAL, name="002_other.json"))

    def test_refuses_a_step_that_needs_a_path_and_has_none(self, tmp_path: Path) -> None:
        raw = {**_MINIMAL, "steps": [{"id": "raw", "action": "http_get"}], "expect": []}
        with pytest.raises(ValueError, match="path"):
            load_scenario(_write(tmp_path, raw))

    def test_refuses_a_step_that_needs_a_session_and_has_none(self, tmp_path: Path) -> None:
        raw = {**_MINIMAL, "steps": [{"id": "one", "action": "get_session"}], "expect": []}
        with pytest.raises(ValueError, match="session_id"):
            load_scenario(_write(tmp_path, raw))


class TestCommittedScenarios:
    """The scenarios in the repository, which are the contract itself."""

    def test_there_is_at_least_one(self) -> None:
        assert load_scenarios(SCENARIO_DIR)

    def test_every_one_loads(self) -> None:
        # Any refusal above, on a committed file, is a broken contract.
        for path in sorted(SCENARIO_DIR.glob("*.json")):
            load_scenario(path)

    def test_every_id_is_unique(self) -> None:
        ids = [scenario.id for scenario in load_scenarios(SCENARIO_DIR)]
        assert len(ids) == len(set(ids))

    def test_they_come_back_in_order(self) -> None:
        ids = [scenario.id for scenario in load_scenarios(SCENARIO_DIR)]
        assert ids == sorted(ids)

    def test_every_expectation_says_why(self) -> None:
        # A cell that fails at three in the morning is read by someone who did
        # not write the scenario. "expected 401, saw 200" is not enough.
        for scenario in load_scenarios(SCENARIO_DIR):
            for expectation in scenario.expectations:
                assert expectation.why, f"{scenario.id}: {expectation.step}.{expectation.path} says no why"
