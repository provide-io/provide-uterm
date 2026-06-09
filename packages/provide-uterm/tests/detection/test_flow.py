#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm.detection import FlowEngine, RuleSet


@pytest.fixture
def login_ruleset() -> RuleSet:
    return RuleSet.model_validate(
        {
            "version": "1.0",
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "match": {"pattern": "Enter your name", "match_mode": "contains"},
                    "input_type": "multi_key",
                    "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}],
                },
                {
                    "id": "login.password",
                    "match": {"pattern": "Enter password", "match_mode": "contains"},
                    "negative_match": {"pattern": "Enter your name", "match_mode": "contains"},
                    "input_type": "multi_key",
                },
                {
                    "id": "main.command",
                    "match": {"pattern": "Command [", "match_mode": "contains"},
                    "input_type": "single_key",
                },
            ],
            "flows": [
                {
                    "id": "login",
                    "description": "login flow",
                    "steps": [
                        {
                            "id": "send_name",
                            "kind": "send_keys",
                            "keys": "alice\r",
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                        },
                        {
                            "id": "send_password",
                            "kind": "send_keys",
                            "keys": "secret\r",
                            "expects_prompt": "login.password",
                            "gate_prompts": ["login.password"],
                        },
                        {
                            "id": "done",
                            "kind": "noop",
                            "expects_prompt": "main.command",
                            "gate_prompts": ["main.command"],
                        },
                    ],
                }
            ],
        }
    )


def test_flow_engine_advances_to_matching_action(login_ruleset: RuleSet) -> None:
    engine = FlowEngine(login_ruleset)
    step = engine.advance("login", "Attempt 3\r\nEnter your name:")

    assert step.flow_id == "login"
    assert step.current_prompt_id == "login.name"
    assert step.next_action == "alice\r"
    assert step.done is False
    assert step.kv_data["attempt"] == 3


def test_flow_engine_honors_negative_match(login_ruleset: RuleSet) -> None:
    engine = FlowEngine(login_ruleset)
    step = engine.advance("login", "Enter password:")

    assert step.current_prompt_id == "login.password"
    assert step.next_action == "secret\r"
    assert step.done is False


def test_flow_engine_reports_terminal_stage_done(login_ruleset: RuleSet) -> None:
    engine = FlowEngine(login_ruleset)
    step = engine.advance("login", "Command [TL=00:00]:")

    assert step.current_prompt_id == "main.command"
    assert step.next_action is None
    assert step.done is True


def test_flow_engine_unknown_flow_raises(login_ruleset: RuleSet) -> None:
    engine = FlowEngine(login_ruleset)
    with pytest.raises(ValueError, match="unknown flow"):
        engine.advance("missing", "screen")


def test_flow_engine_no_matching_stage_returns_idle_step(login_ruleset: RuleSet) -> None:
    engine = FlowEngine(login_ruleset)
    step = engine.advance("login", "No prompt here")

    assert step.current_prompt_id is None
    assert step.next_action is None
    assert step.done is False
    assert step.kv_data == {}


def test_flow_engine_uses_expects_prompt_when_gate_prompts_empty(login_ruleset: RuleSet) -> None:
    ruleset = login_ruleset.model_copy(deep=True)
    ruleset.flows[0].steps[0].gate_prompts = []
    engine = FlowEngine(ruleset)
    step = engine.advance("login", "Enter your name:")

    assert step.current_prompt_id == "login.name"
    assert step.next_action == "alice\r"


def test_flow_engine_ignores_unknown_gate_prompt(login_ruleset: RuleSet) -> None:
    ruleset = login_ruleset.model_copy(deep=True)
    ruleset.flows[0].steps[0].gate_prompts = ["missing.prompt"]
    ruleset.flows[0].steps[0].expects_prompt = None
    engine = FlowEngine(ruleset)
    step = engine.advance("login", "Enter your name:")

    assert step.current_prompt_id is None
    assert step.next_action is None


def test_flow_engine_ignores_step_without_prompt_candidates(login_ruleset: RuleSet) -> None:
    ruleset = login_ruleset.model_copy(deep=True)
    ruleset.flows[0].steps[0].gate_prompts = []
    ruleset.flows[0].steps[0].expects_prompt = None
    engine = FlowEngine(ruleset)
    step = engine.advance("login", "Enter your name:")

    assert step.current_prompt_id is None
    assert step.next_action is None


def test_flow_engine_terminal_last_step_without_keys(login_ruleset: RuleSet) -> None:
    ruleset = login_ruleset.model_copy(deep=True)
    ruleset.flows[0].steps[-1].kind = "wait"
    ruleset.flows[0].steps[-1].keys = None
    engine = FlowEngine(ruleset)
    step = engine.advance("login", "Command [TL=00:00]:", cursor=(3, 4))

    assert step.current_prompt_id == "main.command"
    assert step.next_action is None
    assert step.done is True


def test_flow_engine_wait_action_is_not_sendable(login_ruleset: RuleSet) -> None:
    ruleset = login_ruleset.model_copy(deep=True)
    ruleset.flows[0].steps[0].kind = "wait"
    ruleset.flows[0].steps[0].keys = "ignored"
    engine = FlowEngine(ruleset)
    step = engine.advance("login", "Enter your name:")

    assert step.current_prompt_id == "login.name"
    assert step.next_action is None
    assert step.done is False
