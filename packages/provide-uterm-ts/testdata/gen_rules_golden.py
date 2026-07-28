#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the detection rule schema.

Rules are the file an operator writes. They are the only part of detection a
human authors by hand, which makes two things matter more than usual.

**What a rule leaves out is filled in, and the defaults are a contract.** A
prompt that names only an id and a match still has to produce a working
pattern: multi_key input, a cursor that must be at the end, no exclusion. A
port with different defaults changes the meaning of every rule file already
written against the reference.

**What a rule gets wrong is refused, not guessed at.** The input type, the
match mode and the prompt kind are closed sets. Accepting an unrecognised one
would leave a rule that looks loaded and never fires — the failure an operator
cannot see. The corpus records each refusal.

**A match mode decides how literally the pattern is read.** `regex` is the
author's own expression; `contains` is escaped so a bracket is a bracket; and
`exact` is escaped *and* anchored, so it matches a whole line rather than
appearing anywhere in one. Confusing them silently widens or narrows every
rule that uses them.

The flags default to `MULTILINE | IGNORECASE` and travel with the extraction
rules rather than being applied here — they are for whoever runs the
extraction later, and the number is part of the wire format.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_rules_golden.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from provide.uterm.detection.rules import RegexRule, RuleSet

OUT = Path(__file__).with_name("rules_golden.json")

MINIMAL: dict[str, Any] = {"game": "tw2002", "prompts": [{"id": "cmd", "match": {"pattern": "Command"}}]}

FULL: dict[str, Any] = {
    "version": "2.1",
    "game": "tw2002",
    "metadata": {"author": "someone", "revision": 7},
    "prompts": [
        {
            "id": "login",
            "kind": "login_name",
            "input_type": "multi_key",
            "match": {"pattern": "Enter your name:"},
            "screen": {"expect_cursor_at_end": True, "cursor_row_min": 1, "cursor_row_max": 24},
            "notes": "the first thing it asks",
            "kv_extract": [
                {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int", "required": True},
                {"field": "credits", "regex": r"Credits\s+(\d+)", "validate": {"min": 0}},
            ],
        },
        {
            "id": "pause",
            "kind": "pause",
            "input_type": "any_key",
            "match": {"pattern": "press any key", "match_mode": "contains"},
            "screen": {"expect_cursor_at_end": False},
            "negative_match": {"pattern": "STARDOCK (closed)", "match_mode": "contains"},
        },
        {
            "id": "confirm",
            "kind": "confirm",
            "input_type": "single_key",
            "match": {"pattern": "(Y/N)?", "match_mode": "exact"},
            # An answer the rule already knows, so a flow does not have to
            # spell out what to send at a prompt that only takes one thing.
            "default_action": {"id": "say_yes", "kind": "send_keys", "keys": "Y"},
            # Written as null rather than left out. Both mean "no exclusion",
            # and a port handling only one of them would refuse a file the
            # reference takes.
            "negative_match": None,
        },
    ],
    "menus": [
        {
            "id": "second",
            "title_match": None,
            "prompt_match": {"pattern": "Sub:"},
        },
        {
            "id": "main",
            "title_match": {"pattern": "Main Menu"},
            "prompt_match": {"pattern": "Choice:"},
            "options": [{"key": "1", "label": "Play"}, {"key": "Q", "label": "Quit"}],
            "notes": "the top level",
        },
    ],
    "flows": [
        {
            "id": "login_flow",
            "description": "log in and get to the command prompt",
            "steps": [
                {
                    "id": "send_name",
                    "kind": "send_keys",
                    "keys": "player\r",
                    "expects_prompt": "cmd",
                    "timing": {"min_wait_ms": 100, "max_wait_ms": 5000, "retry_ms": 50},
                    "gate_prompts": ["login"],
                    "block_if_matches": [{"pattern": "banned", "match_mode": "contains"}],
                },
                {"id": "wait_a_bit", "kind": "wait"},
            ],
        }
    ],
}

# (name, mode, pattern) — how literally the pattern is read.
REGEX_MODES: list[tuple[str, str, str]] = [
    ("a regex is the author's own", "regex", r"Command \[TL=[\d:]+\]:"),
    ("contains is escaped", "contains", "Command [TL="),
    ("exact is escaped and anchored", "exact", "Command [TL=00:00:00]:"),
    ("contains with a space", "contains", "STARDOCK is"),
    ("contains with nothing in it", "contains", ""),
    ("exact with nothing in it", "exact", ""),
    ("a regex with nothing in it", "regex", ""),
    ("contains with every metacharacter", "contains", r"a b()[]{}?*+-|^$\.&~#"),
]

# (name, payload) — rule sets the reference refuses.
INVALID: list[tuple[str, Any]] = [
    ("no game", {"prompts": []}),
    ("a prompt with no id", {"game": "g", "prompts": [{"match": {"pattern": "x"}}]}),
    ("a prompt with no match", {"game": "g", "prompts": [{"id": "a"}]}),
    ("a match with no pattern", {"game": "g", "prompts": [{"id": "a", "match": {}}]}),
    (
        "an unknown input type",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "input_type": "chord"}]},
    ),
    ("an unknown prompt kind", {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "kind": "vibes"}]}),
    (
        "an unknown match mode",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x", "match_mode": "fuzzy"}}]},
    ),
    ("a pattern that is not a string", {"game": "g", "prompts": [{"id": "a", "match": {"pattern": 7}}]}),
    ("an id that is not a string", {"game": "g", "prompts": [{"id": 7, "match": {"pattern": "x"}}]}),
    ("prompts that are not a list", {"game": "g", "prompts": {"id": "a"}}),
    (
        "an extraction rule with no field",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "kv_extract": [{"regex": "x"}]}]},
    ),
    (
        "an extraction rule with no regex",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "kv_extract": [{"field": "f"}]}]},
    ),
    ("a menu with no prompt match", {"game": "g", "menus": [{"id": "m"}]}),
    ("a flow step with no kind", {"game": "g", "flows": [{"id": "f", "description": "d", "steps": [{"id": "s"}]}]}),
    (
        "an unknown action kind",
        {"game": "g", "flows": [{"id": "f", "description": "d", "steps": [{"id": "s", "kind": "explode"}]}]},
    ),
    ("a flow with no description", {"game": "g", "flows": [{"id": "f", "steps": []}]}),
    ("not an object at all", ["game"]),
    # A sub-object that is not one. None of these carry a required field, so
    # a port that skipped the shape check would accept them and quietly use
    # every default instead of telling the operator their file is wrong.
    (
        "a screen constraint that is a string",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "screen": "bottom"}]},
    ),
    (
        "a screen constraint that is a list",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "screen": []}]},
    ),
    (
        "a timing block that is a string",
        {
            "game": "g",
            "flows": [{"id": "f", "description": "d", "steps": [{"id": "s", "kind": "wait", "timing": "fast"}]}],
        },
    ),
    (
        "a timing block that is a list",
        {"game": "g", "flows": [{"id": "f", "description": "d", "steps": [{"id": "s", "kind": "wait", "timing": []}]}]},
    ),
    ("a match block that is a string", {"game": "g", "prompts": [{"id": "a", "match": "Command"}]}),
    ("a number where a string belongs", {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "notes": 7}]}),
    # A number is not coerced to a string, only the other way round. A port
    # stringifying these would turn a typo into a version or a type that
    # nothing downstream recognises.
    ("a version that is a number", {"game": "g", "version": 7}),
    (
        "a gate prompt that is not a string",
        {
            "game": "g",
            "flows": [{"id": "f", "description": "d", "steps": [{"id": "s", "kind": "wait", "gate_prompts": [7]}]}],
        },
    ),
    (
        "an extraction type that is a number",
        {
            "game": "g",
            "prompts": [
                {"id": "a", "match": {"pattern": "x"}, "kv_extract": [{"field": "f", "regex": "r", "type": 7}]}
            ],
        },
    ),
]

# (name, payload) — rule sets the reference *accepts* by coercing. Pydantic
# reads a numeric string as a number in its default lax mode, so a rules file
# quoting its numbers still loads. A port refusing them would reject files the
# reference takes.
COERCED: list[tuple[str, Any]] = [
    (
        "a quoted cursor row",
        {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x"}, "screen": {"cursor_row_min": "5"}}]},
    ),
    ("quoted flags", {"game": "g", "prompts": [{"id": "a", "match": {"pattern": "x", "flags": "10"}}]}),
    (
        "a quoted timing",
        {
            "game": "g",
            "flows": [
                {"id": "f", "description": "d", "steps": [{"id": "s", "kind": "wait", "timing": {"retry_ms": "50"}}]}
            ],
        },
    ),
]


def _refusal(payload: Any) -> str:
    """The kind of error the reference gives for a rule set it will not take."""
    try:
        RuleSet.model_validate(payload)
    except Exception as exc:
        return type(exc).__name__
    raise AssertionError(f"expected {payload!r} to be refused")


def main() -> int:
    """Write the golden corpus and report the case count."""
    minimal = RuleSet.model_validate(MINIMAL)
    full = RuleSet.model_validate(FULL)
    empty = RuleSet.model_validate({"game": "none"})

    corpus = {
        "minimal_input": MINIMAL,
        "minimal_dump": minimal.model_dump(),
        "minimal_patterns": minimal.to_prompt_patterns(),
        "full_input": FULL,
        "full_dump": full.model_dump(),
        "full_patterns": full.to_prompt_patterns(),
        "empty_dump": empty.model_dump(),
        "empty_patterns": empty.to_prompt_patterns(),
        "regex_modes": [
            {
                "name": name,
                "mode": mode,
                "pattern": pattern,
                "regex": RegexRule(pattern=pattern, match_mode=mode).to_regex(),
            }
            for name, mode, pattern in REGEX_MODES
        ],
        "invalid": [{"name": name, "payload": payload, "error": _refusal(payload)} for name, payload in INVALID],
        "coerced": [
            {"name": name, "payload": payload, "dump": RuleSet.model_validate(payload).model_dump()}
            for name, payload in COERCED
        ],
        "default_flags": re.MULTILINE | re.IGNORECASE,
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(REGEX_MODES)} modes, {len(INVALID)} refusals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
