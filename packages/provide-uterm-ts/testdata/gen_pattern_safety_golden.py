#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript pattern-safety port.

This validator is what stops a caller pinning the event loop. The guard regex
it protects is ``re.search``ed against a full screen inside the hijack poll
loop, so a pattern like ``(a+)+`` is not a slow query — it is a denial of
service against every other session on the hub.

The rule it enforces is narrow and worth stating exactly: a quantifier applied
to a group is rejected when that group already contained a quantifier, or
contained an alternation. Both conditions propagate outwards, so
``(?=(a+))+`` and ``((a|b))+`` are caught too. Everything else compiles.

Two details are easy to lose in a reimplementation and are recorded here in
force. Group prefixes — ``(?:``, ``(?=``, ``(?<=``, ``(?P<name>`` — are
skipped so the marker characters do not count as content. And a counted
quantifier is only a quantifier if it *looks* like one: ``{2,3}`` is,
``{foo}`` is not, and CPython decides that with ``str.isdigit()``, which
accepts non-ASCII digits that a JavaScript ``\\d`` check would reject.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pattern_safety_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.hub.event_bus import _validate_pattern_safety

OUT = Path(__file__).with_name("pattern_safety_golden.json")

PATTERNS: list[tuple[str, str]] = [
    # -- plain patterns that must keep working ------------------------------
    ("empty", ""),
    ("literal", "hello"),
    ("single quantifier", "a+"),
    ("star", "a*"),
    ("optional", "a?"),
    ("character class", "[a-z]+"),
    ("class containing a paren", "[()]+"),
    ("class containing a bracket", "[]]+"),
    # A class inside a group: its contents are literals, so neither the
    # alternation nor the quantifier inside it belongs to the group.
    ("group wrapping a class with a pipe", "([|])+"),
    ("group wrapping a class with a plus", "([a+])+"),
    ("group wrapping a class with parens", "([()])+"),
    ("escaped paren", r"\(a\)+"),
    ("escaped backslash", r"\\+"),
    ("anchored prompt", r"^\$ $"),
    ("group without a quantifier", "(abc)"),
    ("quantified plain group", "(abc)+"),
    ("alternation at top level", "a|b"),
    ("alternation, unquantified group", "(a|b)"),
    ("counted quantifier", "a{2,3}"),
    ("counted group", "(abc){2}"),
    ("open-ended count", "a{2,}"),
    ("dot star", ".*"),
    ("realistic prompt guard", r"(?m)^[\w.-]+@[\w.-]+:.*[$#] $"),
    # -- the unsafe shapes ---------------------------------------------------
    ("nested quantifier", "(a+)+"),
    ("nested star", "(a*)*"),
    ("nested counted", "(a+){2}"),
    ("quantified alternation", "(a|b)+"),
    ("quantified alternation, counted", "(a|b){3}"),
    ("nested through a non-capturing group", "(?:a+)+"),
    ("nested through a lookahead", "(?=(a+))+"),
    ("nested through a double group", "((a+))+"),
    ("alternation through a double group", "((a|b))+"),
    ("alternation through a named group", "(?P<x>a|b)+"),
    ("nested through a lookbehind", "(?<=(a+))+"),
    ("negative lookahead", "(?!(a+))+"),
    ("nested through a negative lookbehind", "(?<!(a+))+"),
    ("unterminated named group", "(?P<x"),
    ("unterminated named group with a quantifier", "(?P<x a+)+"),
    # -- boundary cases around the counted-quantifier sniff ------------------
    ("brace that is not a quantifier", "a{foo}"),
    ("unclosed brace", "a{2"),
    ("empty braces", "a{}"),
    ("brace with a trailing comma", "a{2,}"),
    ("brace with only a comma", "a{,}"),
    ("brace after a group, not a quantifier", "(a+){foo}"),
    ("brace after a group, counted", "(a+){1,2}"),
    # str.isdigit() is not ASCII-only: these are digits to CPython and would
    # be rejected by a JavaScript \d test, which changes whether the brace
    # counts as a quantifier at all.
    ("arabic-indic digit count", "(a+){٣}"),
    ("superscript digit count", "(a+){²}"),
    # RUF001: the confusable character is the point of the case.
    ("fullwidth digit count", "(a+){３}"),  # noqa: RUF001
    # -- structural oddities -------------------------------------------------
    ("unbalanced close paren", "a)+"),
    ("unbalanced open paren", "(a+"),
    ("inline flags", "(?i)a+"),
    ("quantifier with no preceding token", "+"),
    ("escaped quantifier after a group", r"(a+)\+"),
    ("literal resets the group memory", "(a+)x+"),
    ("alternation outside the group", "(a)|b+"),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    records: list[dict[str, Any]] = []
    for name, pattern in PATTERNS:
        try:
            _validate_pattern_safety(pattern)
            records.append({"name": name, "pattern": pattern, "safe": True, "error": None})
        except ValueError as exc:
            records.append({"name": name, "pattern": pattern, "safe": False, "error": str(exc)})

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_pattern_safety_golden.py",
        "patterns": records,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    unsafe = sum(1 for record in records if not record["safe"])
    print(f"wrote {OUT} ({len(records)} patterns, {unsafe} rejected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
