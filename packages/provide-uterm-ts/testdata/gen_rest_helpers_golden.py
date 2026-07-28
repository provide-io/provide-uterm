#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript rest-helpers port.

These are the prompt guards a REST caller attaches to a keystroke send: "type
this, then wait until the screen looks like *that*". Getting them wrong in
either direction is bad — a guard that matches too eagerly returns before the
command has run, and one that never matches hangs the caller until timeout.

``snapshot_matches`` is recorded in full because its combination rules are
asymmetric. An absent snapshot never matches. An empty prompt-id is not a
constraint at all rather than a constraint on emptiness. And when both guards
are given, both must hold.

``compile_expect_regex`` is the refusal side: too long, unsafe, or
uncompilable, each with its own ``kind`` that callers surface. The order of
those checks matters — an over-long pattern is refused before it is ever
handed to the ReDoS validator, so an attacker cannot spend the validator's
time on a megabyte of input.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_rest_helpers_golden.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.rest_helpers import (
    MAX_EXPECT_REGEX_LEN,
    PromptRegexError,
    compile_expect_regex,
    extract_prompt_id,
    snapshot_matches,
)

OUT = Path(__file__).with_name("rest_helpers_golden.json")

# (name, snapshot)
PROMPT_ID_CASES: list[tuple[str, Any]] = [
    ("absent snapshot", None),
    ("empty snapshot", {}),
    ("no detection", {"screen": "hello"}),
    ("detection present", {"prompt_detected": {"prompt_id": "bash"}}),
    ("detection with an empty id", {"prompt_detected": {"prompt_id": ""}}),
    ("detection with a non-string id", {"prompt_detected": {"prompt_id": 7}}),
    ("detection with a null id", {"prompt_detected": {"prompt_id": None}}),
    ("detection with no id", {"prompt_detected": {"confidence": 0.9}}),
    ("detection that is not a dict", {"prompt_detected": "bash"}),
    ("detection that is a list", {"prompt_detected": [{"prompt_id": "bash"}]}),
]

# (name, snapshot, expect_prompt_id, expect_regex source)
MATCH_CASES: list[tuple[str, Any, str | None, str | None]] = [
    ("no snapshot, no guards", None, None, None),
    ("no snapshot, with guards", None, "bash", "ready"),
    ("snapshot, no guards", {"screen": "anything"}, None, None),
    ("empty snapshot, no guards", {}, None, None),
    ("prompt id matches", {"prompt_detected": {"prompt_id": "bash"}}, "bash", None),
    ("prompt id differs", {"prompt_detected": {"prompt_id": "zsh"}}, "bash", None),
    ("prompt id absent", {"screen": "x"}, "bash", None),
    ("empty prompt id is not a constraint", {"screen": "x"}, "", None),
    ("regex matches the screen", {"screen": "system ready\n$ "}, None, "ready"),
    ("regex misses", {"screen": "still booting"}, None, "ready"),
    ("regex against an absent screen", {}, None, "ready"),
    # An absent screen is the empty string, not the word "undefined": a port
    # that stringifies the missing value directly would match this guard.
    ("guard matching the word undefined", {}, None, "^undefined$"),
    ("guard matching the word none", {}, None, "^none$"),
    ("regex against a non-string screen", {"screen": 42}, None, "42"),
    ("regex is case-insensitive", {"screen": "SYSTEM READY"}, None, "ready"),
    ("regex is multiline", {"screen": "one\nready"}, None, "^ready"),
    ("both guards hold", {"prompt_detected": {"prompt_id": "bash"}, "screen": "ready"}, "bash", "ready"),
    ("only the prompt id holds", {"prompt_detected": {"prompt_id": "bash"}, "screen": "busy"}, "bash", "ready"),
    ("only the regex holds", {"prompt_detected": {"prompt_id": "zsh"}, "screen": "ready"}, "bash", "ready"),
]

# (name, pattern)
COMPILE_CASES: list[tuple[str, str | None]] = [
    ("absent", None),
    ("empty", ""),
    ("simple", "ready"),
    ("anchored", "^\\$ $"),
    ("at the length limit", "a" * MAX_EXPECT_REGEX_LEN),
    ("one over the length limit", "a" * (MAX_EXPECT_REGEX_LEN + 1)),
    ("far over the length limit", "a" * 10_000),
    ("unsafe", "(a+)+"),
    ("unsafe alternation", "(a|b)+"),
    ("unsafe but over-long", "(a+)+" + "b" * MAX_EXPECT_REGEX_LEN),
    ("uncompilable", "("),
    ("uncompilable class", "[z-a]"),
]


def _match(snapshot: Any, expect_prompt_id: str | None, regex_source: str | None) -> bool:
    """Run one snapshot_matches case with the poll loop's own flags."""
    compiled = None if regex_source is None else re.compile(regex_source, re.IGNORECASE | re.MULTILINE)
    return snapshot_matches(snapshot, expect_prompt_id=expect_prompt_id, expect_regex=compiled)


def _compile_record() -> list[dict[str, Any]]:
    """What compile_expect_regex accepts, and how it refuses the rest."""
    records = []
    for name, pattern in COMPILE_CASES:
        record: dict[str, Any] = {"name": name, "pattern": pattern, "length": None if pattern is None else len(pattern)}
        try:
            compiled = compile_expect_regex(pattern, flags=re.IGNORECASE | re.MULTILINE)
            record["ok"] = True
            record["is_none"] = compiled is None
        except PromptRegexError as exc:
            record["ok"] = False
            record["kind"] = exc.kind
            record["message"] = str(exc)
            record["max_length"] = exc.max_length
        records.append(record)
    return records


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_rest_helpers_golden.py",
        "max_expect_regex_len": MAX_EXPECT_REGEX_LEN,
        "prompt_ids": [
            {"name": name, "snapshot": snapshot, "prompt_id": extract_prompt_id(snapshot)}
            for name, snapshot in PROMPT_ID_CASES
        ],
        "matches": [
            {
                "name": name,
                "snapshot": snapshot,
                "expect_prompt_id": expect_prompt_id,
                "expect_regex": regex_source,
                "matched": _match(snapshot, expect_prompt_id, regex_source),
            }
            for name, snapshot, expect_prompt_id, regex_source in MATCH_CASES
        ],
        "compiles": _compile_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(MATCH_CASES)} match cases, {len(COMPILE_CASES)} compile cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
