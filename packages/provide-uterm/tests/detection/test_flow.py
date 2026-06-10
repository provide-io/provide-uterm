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


def test_flow_engine_prefers_tail_prompt_over_stale_scrollback(login_ruleset: RuleSet) -> None:
    """A stale earlier-step prompt left in scrollback must not beat the current
    prompt at the tail (cursor region). Without this, advance() returns the
    first flow step whose prompt matches anywhere, so scrollback wins."""
    engine = FlowEngine(login_ruleset)
    # 'Enter your name' (step 0) is stale scrollback; 'Command [' (step 2) is current.
    step = engine.advance("login", "Enter your name\r\nalice\r\nCommand [TL=00:00]:")

    assert step.current_prompt_id == "main.command"
    assert step.done is True


def test_flow_engine_tail_preference_keeps_single_match(login_ruleset: RuleSet) -> None:
    """When only one prompt matches, tail-preference is a no-op."""
    engine = FlowEngine(login_ruleset)
    step = engine.advance("login", "Enter your name:")
    assert step.current_prompt_id == "login.name"


def test_flow_engine_handles_end_anchored_prompt_with_trailing_blank_lines() -> None:
    """Regression: an end-anchored prompt (\\Z/$) can match the detector's tail
    region while finding nothing in the full screen when trailing blank lines
    shift the anchor. advance() must treat it as tail-most, not crash on an
    empty finditer (max() ValueError)."""
    ruleset = RuleSet.model_validate(
        {
            "version": "1.0",
            "game": "test",
            "prompts": [
                {
                    "id": "cmd",
                    "match": {"pattern": r"Command \[.*\Z", "match_mode": "regex"},
                    "input_type": "single_key",
                }
            ],
            "flows": [
                {
                    "id": "f",
                    "description": "x",
                    "steps": [{"id": "done", "kind": "noop", "expects_prompt": "cmd", "gate_prompts": ["cmd"]}],
                }
            ],
        }
    )
    engine = FlowEngine(ruleset)
    step = engine.advance("f", "Command [TL=00:00]:\n\n")
    assert step.current_prompt_id == "cmd"


def test_flow_engine_keeps_earlier_step_when_it_is_the_tail_prompt(login_ruleset: RuleSet) -> None:
    """When an earlier flow step's prompt is the live tail prompt and a later
    step's prompt is only in scrollback, the earlier (tail-most) one is kept."""
    engine = FlowEngine(login_ruleset)
    # 'Command [' (step 2) is stale scrollback; 'Enter your name' (step 0) is live.
    step = engine.advance("login", "Command [TL=00:00]:\r\nx\r\nEnter your name:")
    assert step.current_prompt_id == "login.name"


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
