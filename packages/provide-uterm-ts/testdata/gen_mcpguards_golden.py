#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the MCP input guards.

Every MCP tool funnels caller-supplied — which here means LLM-supplied —
input through these before it reaches the server, so two things are recorded:

* **The structural ReDoS denylist.** A ``viewer`` may hand ``session_watch``
  a regex that this process then compiles with the standard engine, which has
  no time bound. A length cap does not save a short pathological pattern like
  ``(a+)+$``, so the classic exponential shapes are refused by shape:
  a quantified group whose body is itself quantified, and a quantified
  backreference. It is a denylist, not a proof — what it does *not* catch is
  recorded too, so nobody reads it as one.
* **The rejection contracts.** A bad pattern or a bad id comes back as a
  structured refusal rather than an exception, because an exception reaches
  the caller as a tool error and says more about this process than a refusal
  does.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_mcpguards_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.ai import server_validators as validators
from provide.uterm.ai.constants import MAX_USER_PATTERN_LEN
from provide.uterm.ai.patterns import has_catastrophic_construct

OUT = Path(__file__).resolve().parent / "mcpguards_golden.json"

PATTERNS: list[tuple[str, str]] = [
    # --- the shapes the guard exists for ---------------------------------
    ("a quantified group of a quantified atom", "(a+)+"),
    ("a starred group of a starred atom", "(a*)*"),
    ("a starred group of a plus atom", "(a+)*"),
    ("a quantified group of a starred atom", "(a*)+"),
    ("a quantified group of a word run", r"(\w+)+"),
    ("a counted group of a quantified atom", "(a+){2}"),
    ("the classic, anchored", "(a+)+$"),
    ("a nested quantifier further in", "^x(a+)+y$"),
    ("a lazy quantifier inside a quantified group", "(a+?)+"),
    ("a lazy star inside a quantified group", "(a*?)+"),
    ("a counted body inside a quantified group", "(a{2,3})+"),
    ("a group of a group of a quantified atom", "((a+)+)"),
    ("a backreference, quantified", r"(a)\1+"),
    ("a backreference, starred", r"(a)\1*"),
    ("a backreference, counted", r"(a)\1{2,5}"),
    ("a two-digit backreference, quantified", r"(a)\12+"),
    ("a backreference alone in a quantified group", r"(a)(\1)+"),
    ("a two-digit backreference alone in a quantified group", r"(a)(\10)+"),
    ("a ten-group backreference, quantified", r"(a)\10+"),
    ("a quantifier alone in a lazy group", "(+?)+"),
    # --- shapes it must not refuse ---------------------------------------
    ("nothing at all", ""),
    ("a literal", "hello"),
    ("a quantified atom", "a+"),
    ("a quantified group of a literal", "(ab)+"),
    ("a quantified group of an alternation", "(ab|cd)+"),
    ("a group that is not quantified", "(a+)"),
    ("an empty group, quantified", "()+"),
    ("an empty group, starred", "()*"),
    ("a non-capturing group, quantified", "(?:ab)+"),
    ("a group followed by a literal", "(a+)b"),
    ("a group followed by a question mark", "(a+)?"),
    ("a character class, quantified", "[a-z]+"),
    ("a quantified group of a class", "([a-z])+"),
    ("an escaped quantifier inside a group", r"(a\*)+"),
    ("an escaped plus inside a group", r"(a\+)+"),
    ("escaped parentheses", r"\(a+\)+"),
    ("an unclosed group", "(a+"),
    ("an unopened group", "a+)"),
    ("an unopened group, quantified", "a+)+"),
    ("a null escape, quantified", r"\0+"),
    ("a null escape alone in a quantified group", r"(\0)+"),
    ("an optional atom in a quantified group", "(a?)+"),
    ("an escaped star alone in a quantified group", r"(\*)+"),
    ("a bare backreference", r"(a)\1"),
    ("an escape that is not a backreference", r"\d+"),
    ("a prompt somebody would really write", r"^\$ $"),
    ("a login prompt somebody would really write", r"(?i)password:\s*$"),
    # --- the residual risk, recorded so nobody reads this as a proof -----
    ("an overlapping alternation, which it does not catch", "(a|a)*"),
    ("an overlapping alternation of runs", "(a|aa)+"),
    ("adjacent quantifiers, which it does not catch", "a+a+$"),
]

# Patterns fed to the compiling guard, where length and validity also matter.
COMPILED: list[tuple[str, str]] = [
    ("an ordinary pattern", "^ready$"),
    ("nothing at all", ""),
    ("a pattern at the cap", "a" * MAX_USER_PATTERN_LEN),
    ("a pattern one past the cap", "a" * (MAX_USER_PATTERN_LEN + 1)),
    ("a pattern far past the cap", "a" * (MAX_USER_PATTERN_LEN * 4)),
    ("a catastrophic pattern under the cap", "(a+)+$"),
    ("a catastrophic pattern over the cap", "(a+)+" + "b" * MAX_USER_PATTERN_LEN),
    ("a pattern the engine refuses", "(unclosed"),
    ("a repeat with nothing to repeat", "*"),
    ("a class nobody closed", "[a-"),
    ("a group nobody named properly", "(?P<>x)"),
    ("a backreference to a group that is not there", r"(a)\2"),
]

IDS: list[tuple[str, str, str]] = [
    ("an ordinary id", "worker-1", "worker_id"),
    ("an id with a dot", "worker.1", "worker_id"),
    ("an id with an underscore", "worker_1", "worker_id"),
    ("an id that is a uuid", "00000000-0000-0000-0000-000000000000", "hijack_id"),
    ("nothing at all", "", "worker_id"),
    ("this directory", ".", "worker_id"),
    ("the one above", "..", "worker_id"),
    ("three dots, which name nothing special", "...", "worker_id"),
    ("a path", "a/b", "worker_id"),
    ("a path upwards", "../etc", "worker_id"),
    ("an encoded slash", "a%2Fb", "worker_id"),
    ("a query", "a?b=c", "worker_id"),
    ("a space", "a b", "worker_id"),
    ("a newline", "a\nb", "worker_id"),
    ("a null byte", "a\x00b", "worker_id"),
    ("a colon", "a:b", "session_id"),
    ("something that is not ascii", "wörker", "worker_id"),
]


def _compiled(pattern: str) -> dict[str, Any]:
    try:
        compiled = validators._compile_user_pattern(pattern)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"compiled": compiled.pattern}


def main() -> None:
    corpus = {
        "max_pattern_length": MAX_USER_PATTERN_LEN,
        "catastrophic": [
            {"name": name, "pattern": pattern, "refused": has_catastrophic_construct(pattern)}
            for name, pattern in PATTERNS
        ],
        "compiled": [{"name": name, "pattern": pattern, **_compiled(pattern)} for name, pattern in COMPILED],
        "pattern_rejections": [
            {"name": name, "pattern": pattern, "rejection": validators._reject_bad_pattern(pattern)}
            for name, pattern in COMPILED
        ],
        # No pattern is not a bad pattern: a tool that was asked for no filter
        # has nothing to refuse.
        "no_pattern_rejection": validators._reject_bad_pattern(None),
        "no_pattern_compiled": [
            validators._compiled_pattern_or_rejection(None)[0],
            validators._compiled_pattern_or_rejection(None)[1],
        ],
        "compiled_or_rejection": [
            {
                "name": name,
                "pattern": pattern,
                "compiled": None
                if validators._compiled_pattern_or_rejection(pattern)[0] is None
                else validators._compiled_pattern_or_rejection(pattern)[0].pattern,
                "rejection": validators._compiled_pattern_or_rejection(pattern)[1],
            }
            for name, pattern in COMPILED
        ],
        "ids": [
            {"name": name, "value": value, "kind": kind, "rejection": validators._reject_bad_id(value, kind)}
            for name, value, kind in IDS
        ],
        "id_default_kind": validators._reject_bad_id("a/b"),
        # Several ids at once: whichever is bad first is the one reported, so
        # a caller fixes them in the order they were given.
        "id_pairs": [
            {
                "name": "both good",
                "pairs": [["w-1", "worker_id"], ["h-1", "hijack_id"]],
                "rejection": validators._reject_bad_ids(("w-1", "worker_id"), ("h-1", "hijack_id")),
            },
            {
                "name": "the first is bad",
                "pairs": [["a/b", "worker_id"], ["h-1", "hijack_id"]],
                "rejection": validators._reject_bad_ids(("a/b", "worker_id"), ("h-1", "hijack_id")),
            },
            {
                "name": "the second is bad",
                "pairs": [["w-1", "worker_id"], ["c/d", "hijack_id"]],
                "rejection": validators._reject_bad_ids(("w-1", "worker_id"), ("c/d", "hijack_id")),
            },
            {
                "name": "both are bad",
                "pairs": [["a/b", "worker_id"], ["c/d", "hijack_id"]],
                "rejection": validators._reject_bad_ids(("a/b", "worker_id"), ("c/d", "hijack_id")),
            },
            {"name": "nothing to check", "pairs": [], "rejection": validators._reject_bad_ids()},
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    refused = sum(1 for entry in corpus["catastrophic"] if entry["refused"])
    print(f"wrote {OUT} ({refused} of {len(PATTERNS)} patterns refused)")


if __name__ == "__main__":
    main()
