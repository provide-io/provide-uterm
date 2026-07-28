#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for declarative flow execution.

A flow is a scripted conversation — log in, answer the prompts, stop. The
engine looks at one screen and says what to send next, so a wrong answer here
types the wrong thing into a live terminal.

**Whichever prompt sits closest to the bottom wins.** Several of a flow's
steps may match at once, because a screen holds scrollback and an earlier
step's prompt is often still visible above the live one. Ranking by how far
down the match *ends* keeps the live prompt winning; taking the first
matching step instead would answer a prompt that scrolled past minutes ago.

**A tie on the same line goes to the match that starts earlier.** Two rules
can end at the same column — a whole-line ``Enter your password:`` and a
generic ``password[?:]\\s*$`` suffix. The anchored, longer match has to win, or
the vague suffix steals the resolution and the flow answers as though it were
at a different prompt. That is why the ranking key is ``(end, -start)`` and
not just the tail-most start.

**A step's gate prompts and its expected prompt are both candidates**, the
expected one appended last, so gate order is preserved and the prompt a step
is waiting for is still recognised.

**A flow ends on a no-op, or on a last step with nothing to send.** Anything
else has keys to send, and a flow that thought it was finished would leave a
session sitting at a prompt.

**Detectors are cached per prompt-id set.** Not an optimisation detail: login
polls this every fifth of a second, and rebuilding a detector each time
recompiles every pattern thousands of times. The corpus checks the same object
comes back, because a cache that misses is invisible until something is slow.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_flow_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.detection.flow import FlowEngine
from provide.uterm.detection.rules import RuleSet

OUT = Path(__file__).with_name("flow_golden.json")

RULES: dict[str, Any] = {
    "game": "tw2002",
    "prompts": [
        {
            "id": "login_name",
            "match": {"pattern": r"Enter your name:"},
            "kv_extract": [{"field": "attempt", "regex": r"attempt (\d+)", "type": "int"}],
        },
        {"id": "login_pass", "match": {"pattern": r"Enter your password:"}},
        # A deliberately vague rule that ends where the anchored one does.
        {"id": "pass_suffix", "match": {"pattern": r"password[?:]\s*$"}},
        {"id": "command", "match": {"pattern": r"Command \[TL=[\d:]+\]:"}},
        {"id": "pause", "match": {"pattern": r"press any key"}},
        # Anchored to a line start, so a position search that is not multiline
        # finds it only on the first line.
        {"id": "line_start", "match": {"pattern": r"^Choice:"}},
        # Lower case in the rule, upper on the screen: the detector is
        # case-sensitive, so this is only ever reached deliberately.
        {"id": "shouty", "match": {"pattern": r"READY TO GO"}},
        {"id": "twice", "match": {"pattern": r"marker"}},
        {"id": "middle", "match": {"pattern": r"middle"}},
        {"id": "above", "match": {"pattern": r"above"}},
        # Two rules anchored to a line start, neither on the first line. Rank
        # them without multiline and both fall back to the tail, tie, and the
        # earlier step wins — which is the wrong prompt.
        {"id": "alpha", "match": {"pattern": r"^Alpha:"}},
        {"id": "beta", "match": {"pattern": r"^Beta:"}},
        # Matches the detector's tail region but not the whole screen once
        # trailing content shifts the anchor.
        {"id": "anchored", "match": {"pattern": r"Ready\Z"}},
    ],
    "flows": [
        {
            "id": "login",
            "description": "log in",
            "steps": [
                {"id": "send_name", "kind": "send_keys", "keys": "player\r", "gate_prompts": ["login_name"]},
                {"id": "send_pass", "kind": "send_keys", "keys": "secret\r", "gate_prompts": ["login_pass"]},
                {"id": "arrive", "kind": "noop", "gate_prompts": ["command"]},
            ],
        },
        {
            "id": "suffix_race",
            "description": "the anchored rule and the vague one",
            "steps": [
                {"id": "vague", "kind": "send_keys", "keys": "v", "gate_prompts": ["pass_suffix"]},
                {"id": "anchored_step", "kind": "send_keys", "keys": "a", "gate_prompts": ["login_pass"]},
            ],
        },
        {
            "id": "expects",
            "description": "a step naming the prompt it waits for",
            "steps": [{"id": "wait", "kind": "send_keys", "keys": "x", "expects_prompt": "command"}],
        },
        {
            "id": "both",
            "description": "gates and an expectation together",
            "steps": [
                {
                    "id": "wait",
                    "kind": "send_keys",
                    "keys": "x",
                    "gate_prompts": ["login_name"],
                    "expects_prompt": "command",
                }
            ],
        },
        {
            "id": "gateless",
            "description": "a step with nothing to wait for",
            "steps": [{"id": "wait", "kind": "send_keys", "keys": "x"}],
        },
        {
            "id": "unknown_gate",
            "description": "a step naming a prompt that does not exist",
            "steps": [{"id": "wait", "kind": "send_keys", "keys": "x", "gate_prompts": ["nonexistent"]}],
        },
        {
            "id": "last_without_keys",
            "description": "a final step with nothing to send",
            "steps": [
                {"id": "first", "kind": "send_keys", "keys": "a", "gate_prompts": ["login_name"]},
                {"id": "final", "kind": "wait", "gate_prompts": ["command"]},
            ],
        },
        {
            "id": "middle_without_keys",
            "description": "a middle step with nothing to send",
            "steps": [
                {"id": "middle", "kind": "wait", "gate_prompts": ["login_name"]},
                {"id": "final", "kind": "send_keys", "keys": "z", "gate_prompts": ["command"]},
            ],
        },
        {
            "id": "anchored_flow",
            "description": "an end-anchored rule",
            "steps": [{"id": "wait", "kind": "send_keys", "keys": "r", "gate_prompts": ["anchored"]}],
        },
        {"id": "empty", "description": "no steps at all", "steps": []},
        {
            "id": "reversed",
            "description": "the earlier step's prompt is the lower one",
            "steps": [
                {"id": "lower", "kind": "send_keys", "keys": "L", "gate_prompts": ["command"]},
                {"id": "upper", "kind": "send_keys", "keys": "U", "gate_prompts": ["login_name"]},
            ],
        },
        {
            "id": "tied",
            "description": "two steps gated on the same prompt",
            "steps": [
                {"id": "first", "kind": "send_keys", "keys": "1", "gate_prompts": ["login_name"]},
                {"id": "second", "kind": "send_keys", "keys": "2", "gate_prompts": ["login_name"]},
            ],
        },
        {
            "id": "repeated",
            "description": "a prompt appearing twice on one screen",
            "steps": [{"id": "only", "kind": "send_keys", "keys": "m", "gate_prompts": ["twice", "command"]}],
        },
        {
            "id": "line_anchored",
            "description": "a rule anchored to a line start",
            "steps": [{"id": "only", "kind": "send_keys", "keys": "c", "gate_prompts": ["line_start"]}],
        },
        {
            "id": "order_matters",
            "description": "two prompts on one line, gate order deciding",
            "steps": [
                {
                    "id": "only",
                    "kind": "send_keys",
                    "keys": "o",
                    "gate_prompts": ["pass_suffix"],
                    "expects_prompt": "login_pass",
                }
            ],
        },
        {
            "id": "order_reversed",
            "description": "the same two, the other way round",
            "steps": [
                {
                    "id": "only",
                    "kind": "send_keys",
                    "keys": "o",
                    "gate_prompts": ["login_pass"],
                    "expects_prompt": "pass_suffix",
                }
            ],
        },
        {
            "id": "noop_not_last",
            "description": "a no-op that is not the last step",
            "steps": [
                {"id": "stop", "kind": "noop", "gate_prompts": ["login_name"]},
                {"id": "after", "kind": "send_keys", "keys": "a", "gate_prompts": ["command"]},
            ],
        },
        {
            "id": "noop_with_keys",
            "description": "a no-op that still carries keys",
            "steps": [{"id": "stop", "kind": "noop", "keys": "never", "gate_prompts": ["login_name"]}],
        },
        {
            "id": "wait_with_keys",
            "description": "a middle wait step carrying keys",
            "steps": [
                {"id": "waiting", "kind": "wait", "keys": "unsent", "gate_prompts": ["login_name"]},
                {"id": "after", "kind": "send_keys", "keys": "a", "gate_prompts": ["command"]},
            ],
        },
        {
            "id": "lowest_of_many",
            "description": "one pattern matching twice, against a competitor between them",
            "steps": [
                {"id": "repeat", "kind": "send_keys", "keys": "R", "gate_prompts": ["twice"]},
                {"id": "between", "kind": "send_keys", "keys": "B", "gate_prompts": ["middle"]},
            ],
        },
        {
            "id": "anchor_vs_earlier",
            "description": "an end-anchored rule against one that matches above it",
            "steps": [
                {"id": "anchor", "kind": "send_keys", "keys": "A", "gate_prompts": ["anchored"]},
                {"id": "earlier", "kind": "send_keys", "keys": "E", "gate_prompts": ["above"]},
            ],
        },
        {
            "id": "line_vs_earlier",
            "description": "a line-anchored rule against one that matches above it",
            "steps": [
                {"id": "choice", "kind": "send_keys", "keys": "C", "gate_prompts": ["line_start"]},
                {"id": "earlier", "kind": "send_keys", "keys": "E", "gate_prompts": ["above"]},
            ],
        },
        {
            "id": "case_vs_earlier",
            "description": "a case-sensitive rule against one that matches between its occurrences",
            "steps": [
                {"id": "shout", "kind": "send_keys", "keys": "S", "gate_prompts": ["shouty"]},
                {"id": "earlier", "kind": "send_keys", "keys": "E", "gate_prompts": ["middle"]},
            ],
        },
        {
            "id": "two_line_anchored",
            "description": "two line-anchored rules below the first line",
            "steps": [
                {"id": "a", "kind": "send_keys", "keys": "A", "gate_prompts": ["alpha"]},
                {"id": "b", "kind": "send_keys", "keys": "B", "gate_prompts": ["beta"]},
            ],
        },
        {
            "id": "middle_send_no_keys",
            "description": "a middle send step with nothing to send",
            "steps": [
                {"id": "empty_send", "kind": "send_keys", "gate_prompts": ["login_name"]},
                {"id": "after", "kind": "send_keys", "keys": "a", "gate_prompts": ["command"]},
            ],
        },
        {
            "id": "terminal_send",
            "description": "a last send step with nothing to send",
            "steps": [{"id": "only", "kind": "send_keys", "gate_prompts": ["login_name"]}],
        },
        {
            "id": "anchored_trailing",
            "description": "an end-anchored rule with trailing content",
            "steps": [{"id": "wait", "kind": "send_keys", "keys": "r", "gate_prompts": ["anchored"]}],
        },
    ],
}

SCROLLBACK = "\n".join(
    [
        "Enter your name: player",
        "welcome back",
        "attempt 3",
        "Command [TL=00:00:00]:? ",
    ]
)

# (name, flow id, screen, cursor)
CASES: list[tuple[str, str, str, Any]] = [
    ("at the first prompt", "login", "Enter your name: ", None),
    ("at the second prompt", "login", "Enter your name: player\nEnter your password: ", None),
    ("at the last prompt", "login", "Command [TL=00:00:00]:? ", None),
    # The live prompt is at the bottom and an earlier step's prompt is still
    # visible above it.
    ("a stale prompt above a live one", "login", SCROLLBACK, None),
    ("nothing matches", "login", "just some output", None),
    ("an empty screen", "login", "", None),
    # Both rules end at the same column on the same line.
    ("two rules ending on the same line", "suffix_race", "Enter your password:", None),
    ("a step naming its expected prompt", "expects", "Command [TL=00:00:00]:? ", None),
    ("gates and an expectation together, the gate matching", "both", "Enter your name: ", None),
    ("gates and an expectation together, the expectation matching", "both", "Command [TL=00:00:00]:? ", None),
    ("a step with nothing to wait for", "gateless", "Enter your name: ", None),
    ("a step naming a prompt that does not exist", "unknown_gate", "Enter your name: ", None),
    ("a final step with nothing to send", "last_without_keys", "Command [TL=00:00:00]:? ", None),
    ("a middle step with nothing to send", "middle_without_keys", "Enter your name: ", None),
    ("a no-op step", "login", "Command [TL=00:00:00]:? ", None),
    ("a flow with no steps", "empty", "anything", None),
    ("an end-anchored rule at the tail", "anchored_flow", "Ready", None),
    ("extracted values travel with the step", "login", "attempt 3\nEnter your name: ", None),
    ("the last extracted value wins", "login", "attempt 1\nattempt 7\nEnter your name: ", None),
    ("a prompt with nothing to extract", "login", "Command [TL=00:00:00]:? ", None),
    ("the earlier step's prompt is the lower one", "reversed", "Enter your name: x\nCommand [TL=00:00:00]:? ", None),
    ("two steps tied on the same prompt", "tied", "Enter your name: ", None),
    ("a prompt appearing twice on one screen", "repeated", "marker one\nmarker two", None),
    ("a rule anchored to a line start", "line_anchored", "first line\nChoice: ", None),
    ("gate order deciding between two on one line", "order_matters", "Enter your password:", None),
    ("the same two, the other way round", "order_reversed", "Enter your password:", None),
    ("a no-op that is not the last step", "noop_not_last", "Enter your name: ", None),
    ("a no-op that still carries keys", "noop_with_keys", "Enter your name: ", None),
    ("a middle wait step carrying keys", "wait_with_keys", "Enter your name: ", None),
    # The region ends at the last line with content, so the anchored rule
    # matches there and finds nothing in the whole screen.
    ("an end-anchored rule with trailing blank lines", "anchored_trailing", "Ready\n\n", None),
    ("one pattern matching twice, ranked by the lower", "lowest_of_many", "marker one\nmiddle\nmarker two", None),
    ("an end-anchored rule outranks one above it", "anchor_vs_earlier", "above here\nReady\n\n", None),
    ("a line-anchored rule outranks one above it", "line_vs_earlier", "above here\nChoice: ", None),
    # The exact-case occurrence is above a lower-case one; ranking must not
    # reach for the lower-case text the detector would never have matched.
    ("a case-sensitive rule ranked without case folding", "case_vs_earlier", "READY TO GO\nmiddle\nready to go", None),
    ("two line-anchored rules, neither on the first line", "two_line_anchored", "x\nAlpha: 1\nBeta: 2", None),
    ("a middle send step with nothing to send", "middle_send_no_keys", "Enter your name: ", None),
    ("a last send step with nothing to send", "terminal_send", "Enter your name: ", None),
    ("an explicit cursor", "login", "Enter your name: ", (5, 0)),
    ("a cursor above the prompt", "login", "one\ntwo\nEnter your name: ", (0, 0)),
]


def _step(engine: FlowEngine, flow_id: str, screen: str, cursor: Any) -> dict[str, Any]:
    """What the engine decides for one screen."""
    result = engine.advance(flow_id, screen, cursor)
    return {
        "flow_id": result.flow_id,
        "current_prompt_id": result.current_prompt_id,
        "next_action": result.next_action,
        "done": result.done,
        "kv_data": result.kv_data,
    }


def _refusal(engine: FlowEngine) -> str:
    """What the engine says about a flow it does not have."""
    try:
        engine.advance("nonexistent", "screen")
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def main() -> int:
    """Write the golden corpus and report the case count."""
    ruleset = RuleSet.model_validate(RULES)
    engine = FlowEngine(ruleset)

    # The same prompt-id set must come back as the same detector: login polls
    # this every fifth of a second, and rebuilding recompiles every pattern.
    first = engine._detect_prompt({"screen": "Enter your name: ", "screen_hash": "h"}, ["login_name"])
    cached_same_object = (
        engine._detector_cache[("login_name",)] is engine._detector_cache[("login_name",)] and first is not None
    )

    corpus = {
        "rules": RULES,
        "scrollback": SCROLLBACK,
        "cases": [
            {
                "name": name,
                "flow_id": flow_id,
                "screen": screen,
                "cursor": cursor,
                "step": _step(engine, flow_id, screen, cursor),
            }
            for name, flow_id, screen, cursor in CASES
        ],
        "unknown_flow_error": _refusal(engine),
        "cached_same_object": cached_same_object,
        "snapshot": {
            "no_cursor": FlowEngine(ruleset)._snapshot("one\ntwo\n", None),
            "with_cursor": FlowEngine(ruleset)._snapshot("one\ntwo\n", (3, 1)),
            "trailing_space": FlowEngine(ruleset)._snapshot("prompt: ", None),
            "no_trailing_space": FlowEngine(ruleset)._snapshot("prompt:", None),
            "empty": FlowEngine(ruleset)._snapshot("", None),
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
