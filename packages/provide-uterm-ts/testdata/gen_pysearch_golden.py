#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the golden corpus for one-shot `re.search` with explicit flags.

Prompt detection compiles operator-written patterns with ``re.MULTILINE`` and
searches them against a screen, and compiles exclusion patterns with
``re.MULTILINE | re.IGNORECASE``. Two things about that have to survive the
port.

**Search is one-shot.** ECMAScript's global flag makes a compiled pattern
stateful — ``lastIndex`` carries between calls — so the same pattern asked
about the same screen twice answers differently the second time. A detector
built on a global pattern misses every other prompt.

**The two flag sets are not interchangeable.** Positive prompt patterns are
case-sensitive, because prompt authors rely on exact case to tell prompts
apart; exclusions are case-insensitive, because they are broad guards. Swap
them and a rule written to block ``stardock`` stops blocking ``STARDOCK``, or
a prompt written for ``Command:`` starts firing on ``command:``.

``^`` and ``$`` are per-line under MULTILINE in both dialects, which is what
makes a screen — one string of many lines — matchable at all.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pysearch_golden.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

OUT = Path(__file__).with_name("pysearch_golden.json")

SCREEN = "Welcome to the game\nSTARDOCK is closed\nCommand [TL=00:00:00]:? "

# (name, pattern, subject, ignore_case)
CASES: list[tuple[str, str, str, bool]] = [
    ("a literal in the middle", "STARDOCK", SCREEN, False),
    ("the same literal, wrong case", "stardock", SCREEN, False),
    ("the same literal, case insensitive", "stardock", SCREEN, True),
    ("anchored to a line start", r"^Command", SCREEN, False),
    ("anchored to a line start, wrong line", r"^STARDOCK", SCREEN, False),
    ("anchored to the string start only", r"\AWelcome", SCREEN, False),
    ("anchored to a line end", r"closed$", SCREEN, False),
    ("a line end that is not one", r"closed$", "closed now", False),
    ("a prompt with a colon", r"Command \[TL=[\d:]+\]:", SCREEN, False),
    ("a character class", r"[Ww]elcome", SCREEN, False),
    ("no match at all", "nothing here", SCREEN, False),
    ("an empty pattern", "", SCREEN, False),
    ("an empty subject", "anything", "", False),
    ("both empty", "", "", False),
    ("a dot does not cross a line", "game.STARDOCK", SCREEN, False),
    ("an escaped literal", re.escape("[TL=00:00:00]:"), SCREEN, False),
    ("a leading inline flag", "(?i)stardock", SCREEN, False),
    ("case insensitive already", "(?i)stardock", SCREEN, True),
    ("a group", r"(Command|Prompt) \[", SCREEN, False),
    ("a repeated search finds the same place", "o", SCREEN, False),
    # \A and \Z anchor to the whole string even under MULTILINE, where ^ and $
    # anchor per line. ECMAScript has neither: it reads \A as the letter A.
    ("string start beats line start", r"\ASTARDOCK", SCREEN, False),
    ("string end", r"\? \Z", SCREEN, False),
    ("string end on the wrong line", r"closed\Z", SCREEN, False),
    ("string end after a newline", r"closed\Z", "closed\n", False),
    ("an escaped backslash then A", "\\\\A", "\\A", False),
    ("both anchors together", r"\AWelcome[\s\S]*\? \Z", SCREEN, False),
]


# Patterns CPython refuses outright. An anchor is not a class member, so a
# rule written this way is a compile failure rather than a literal letter —
# which is what a port treating it as one would quietly turn it into.
INVALID: list[tuple[str, str]] = [
    ("an anchor inside a character class", r"[\A]"),
    ("an end anchor inside a character class", r"[\Z]"),
]


def _refusal(pattern: str) -> str:
    """The message CPython gives for a pattern it will not compile."""
    try:
        re.compile(pattern)
    except re.error as exc:
        return str(exc)
    raise AssertionError(f"expected {pattern!r} to be refused")


def main() -> int:
    """Write the golden corpus and report the case count."""
    records = []
    for name, pattern, subject, ignore_case in CASES:
        flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
        compiled = re.compile(pattern, flags)
        first = compiled.search(subject)
        # Searched again: the answer must not move. A stateful compiled
        # pattern would return the *next* occurrence here.
        second = compiled.search(subject)
        records.append(
            {
                "name": name,
                "pattern": pattern,
                "subject": subject,
                "ignore_case": ignore_case,
                "matched": first is not None,
                "start": first.start() if first else None,
                "text": first.group(0) if first else None,
                "second_start": second.start() if second else None,
            }
        )
    corpus = {
        "cases": records,
        "invalid": [{"name": name, "pattern": pattern, "error": _refusal(pattern)} for name, pattern in INVALID],
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
