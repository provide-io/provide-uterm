#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for CPython's ``str()`` of a value.

``str(x)`` and JavaScript's ``String(x)`` agree on strings and on most whole
numbers, and disagree on nearly everything else: ``None`` renders as ``None``
rather than ``null``, ``True`` as ``True`` rather than ``true``, and a
whole-valued float keeps the ``.0`` this runtime drops.

That matters wherever a reference module renders an arbitrary value as text
and then *decides* something from it. The case this was written for is
``session_subscribe``, which renders each event's screen and matches a
caller's pattern against the result — so a screen that is ``None`` becomes
the four characters ``None``, and a pattern like ``^N`` fires on it. Reaching
for the host runtime's ``String`` there would have quietly changed which
sessions an agent is told about.

# uv-package: provide-uterm

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pystrvalue_golden.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "pystrvalue_golden.json"

# (name, value) — everything a JSON parser can hand over, plus the numbers it
# can produce that Python spells differently.
VALUES: list[tuple[str, Any]] = [
    ("nothing at all", None),
    ("true", True),
    ("false", False),
    ("a word", "hello"),
    ("nothing written down", ""),
    ("a word that looks like nothing", "None"),
    ("a word with a quote in it", "it's"),
    ("a whole number", 42),
    ("zero", 0),
    ("a negative number", -7),
    ("a very large whole number", 2**53),
    ("a whole-valued float", 1.0),
    ("zero as a float", 0.0),
    ("a negative zero", -0.0),
    ("a fraction", 0.5),
    ("a long fraction", 1 / 3),
    ("a number in exponent form", 1e20),
    ("a small number in exponent form", 1e-7),
    ("the largest whole float", 1e16),
    ("one below it", 1e15),
    ("infinity", math.inf),
    ("negative infinity", -math.inf),
    ("not a number", math.nan),
    ("an empty list", []),
    ("a list of numbers", [1, 2]),
    ("a list of words", ["a", "b"]),
    ("a list holding nothing", [None, True]),
    ("a list of one", [1]),
    ("an empty mapping", {}),
    ("a mapping", {"a": 1}),
    ("a mapping of words", {"a": "b"}),
    ("a mapping of two", {"a": 1, "b": 2}),
    ("a nested list", [[1], [2]]),
    ("a list holding a mapping", [{"a": 1}]),
]


# Strings whose ``repr`` is where the quoting and escaping decisions live.
# These are attacker-chosen in practice — they reach logs and refusals — so a
# control character that is printed rather than escaped moves an operator's
# cursor.
REPRS: list[tuple[str, str]] = [
    ("nothing written down", ""),
    ("a word", "hello"),
    ("a backslash", "a\\b"),
    ("a newline", "a\nb"),
    ("a carriage return", "a\rb"),
    ("a tab", "a\tb"),
    ("a null byte", "a\x00b"),
    ("the lowest control character there is", "\x01"),
    ("the highest control character below space", "\x1f"),
    ("a space, which is not a control character", "a b"),
    ("the delete character", "a\x7fb"),
    ("one below delete", "a\x7eb"),
    ("an apostrophe", "it's"),
    ("a double quote", 'say "hi"'),
    ("both kinds of quote", 'it\'s "fine"'),
    ("only quotes", "''"),
    ("something not ascii", "café"),
    ("something well past ascii", "日本"),
    ("an escape sequence", "\x1b[31m"),
    ("a bell", "a\x07b"),
]


def _is_whole(value: Any) -> bool:
    """Whether a number is one this runtime could not tell from an ``int``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (
        math.isfinite(value)
        and float(value).is_integer()
        and not (isinstance(value, float) and value == 0 and math.copysign(1, value) < 0)
    )


def _carried(value: Any) -> Any:
    """The value as JSON can carry it, or the name of one it cannot."""
    if isinstance(value, float) and not math.isfinite(value):
        return f"<{value}>"
    return value


def main() -> None:
    corpus = {
        # A non-finite float is named rather than written: `json.dumps` spells
        # them `NaN` and `Infinity`, which is not JSON and not something the
        # other side's parser would take.
        "values": [
            {
                "name": name,
                "value": _carried(value),
                "text": str(value),
                # What the same value spells as an `int`, where it has one.
                # A runtime with a single numeric type cannot tell `1.0` from
                # `1`, so this is the spelling it must produce.
                **({"int_text": str(int(value))} if _is_whole(value) else {}),
            }
            for name, value in VALUES
        ],
        "reprs": [{"name": name, "text": text, "repr": repr(text)} for name, text in REPRS],
        "special": {
            "nan": str(math.nan),
            "inf": str(math.inf),
            "-inf": str(-math.inf),
            "-0.0": str(-0.0),
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2) + "\n")
    print(f"wrote {OUT} ({len(VALUES)} values)")


if __name__ == "__main__":
    main()
