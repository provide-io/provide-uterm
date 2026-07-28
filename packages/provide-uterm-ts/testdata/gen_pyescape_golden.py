#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the golden corpus for `re.escape` and lossy UTF-8 encoding.

Two CPython behaviours that a port reaches for without noticing they differ.

**`re.escape` escapes more than the obvious metacharacters.** Since 3.7 it
escapes exactly ``()[]{}?*+-|^$\\.&~#`` and the whitespace characters — so a
space becomes ``\\ ``, which most hand-written escape helpers leave alone. It
matters here because an operator's exclusion rule is escaped before it is
compiled, and the escaped text is recorded in the detector's diagnostics: a
port escaping differently reports a different rule to whoever is debugging,
even when the two happen to match the same screens.

**`str.encode(errors="replace")` substitutes an ASCII question mark**, not the
U+FFFD replacement character that decoding uses. The prompt fingerprint is a
hash of encoded text, so a port reaching for U+FFFD hashes different bytes and
every cache key for a screen containing an unpaired surrogate diverges.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pyescape_golden.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

OUT = Path(__file__).with_name("pyescape_golden.json")

ESCAPE_CASES: list[tuple[str, str]] = [
    ("plain letters", "STARDOCK"),
    ("a space", "STARDOCK is"),
    ("a tab", "a\tb"),
    ("a newline", "a\nb"),
    ("a carriage return", "a\rb"),
    ("a form feed", "a\fb"),
    ("a vertical tab", "a\vb"),
    ("brackets", "[TL=00:00:00]"),
    ("parentheses", "(Y/N)"),
    ("braces", "{a}"),
    ("a question mark", "really?"),
    ("a star", "a*b"),
    ("a plus", "a+b"),
    ("a hyphen", "a-b"),
    ("a pipe", "a|b"),
    ("a caret", "a^b"),
    ("a dollar", "a$b"),
    ("a backslash", "a\\b"),
    ("a dot", "a.b"),
    ("an ampersand", "a&b"),
    ("a tilde", "a~b"),
    ("a hash", "a#b"),
    ("a colon", "a:b"),
    ("a slash", "a/b"),
    ("an equals", "a=b"),
    ("a comma", "a,b"),
    ("an underscore", "a_b"),
    ("digits", "123"),
    ("empty", ""),
    ("unicode", "héllo → ✓"),
    ("everything at once", "a b\tc()[]{}?*+-|^$\\.&~#"),
]

ENCODE_CASES: list[tuple[str, str]] = [
    ("plain ascii", "hello"),
    ("unicode", "héllo → ✓"),
    ("empty", ""),
    ("an emoji", "\U0001f600"),
    # Above the surrogate range rather than inside it. A range check with no
    # upper bound would replace these as though they were broken halves.
    ("a private-use character", "\ue000"),
    ("an arabic ligature", "\ufdfd"),
    ("just past the surrogates", "\ue000\uffff"),
]

# Strings carrying unpaired surrogates cannot travel through JSON, so they are
# described by their code points and rebuilt on both sides.
#
# Only *lone* surrogates appear here. A Python str is a sequence of code
# points, so chr(0xD83D) + chr(0xDE00) is two broken characters; the same two
# units in a JavaScript string are one emoji. There is no way to write that
# case so it means the same thing on both sides, and it is not the behaviour
# under test — a real astral character is covered by the emoji case above,
# written as a literal in both.
SURROGATE_CASES: list[tuple[str, list[int]]] = [
    ("a lone high surrogate", [98, 97, 100, 32, 0xD800, 32, 99]),
    ("a lone low surrogate", [98, 97, 100, 32, 0xDC00, 32, 99]),
    ("two lone surrogates", [0xD800, 0xD800]),
    ("a surrogate at the end", [97, 0xD800]),
    ("a surrogate at the start", [0xD800, 97]),
    # Two low halves. Pair detection that only checked the *second* unit would
    # read these as a pair and emit them as though they were a character.
    ("two low halves", [0xDC00, 0xDC00]),
    ("a low half then a high one", [0xDC00, 0xD800]),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "escape": [{"name": name, "value": value, "escaped": re.escape(value)} for name, value in ESCAPE_CASES],
        "encode": [
            {"name": name, "value": value, "bytes": list(value.encode(errors="replace"))}
            for name, value in ENCODE_CASES
        ],
        "surrogates": [
            {
                "name": name,
                "code_points": points,
                "bytes": list("".join(chr(point) for point in points).encode(errors="replace")),
            }
            for name, points in SURROGATE_CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(ESCAPE_CASES)} escape, {len(SURROGATE_CASES)} surrogate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
