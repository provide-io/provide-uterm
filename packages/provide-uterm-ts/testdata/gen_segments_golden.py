#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for rendering text into segments.

A terminal client interprets ANSI itself. A structured client — a web UI
rendering spans rather than a character grid — needs the same colour
information as data. These segments are parsed back out of the very ANSI the
terminal renders, so the two presentations cannot drift.

**Adjacent runs of the same style are merged and empty runs are dropped.** A
client rendering one span per segment would otherwise emit a span per escape
sequence, and an empty one for every reset that changed nothing.

**A bright colour is the base colour plus bold**, so a client with no separate
bright palette still tells the two apart.

**An extended-colour sequence has its operands skipped rather than read.**
`38;5;196` is one instruction, and a parser that walked into it would read the
196 as another SGR code and paint the text a colour nobody asked for.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_segments_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.render.segments import SEGMENT_COLOR_NAMES, ansi_to_segments, tokens_to_segments

OUT = Path(__file__).with_name("segments_golden.json")

ESC = "\x1b"

# (name, ansi) — what a run of coloured text becomes.
ANSI_CASES: list[tuple[str, str]] = [
    ("plain text", "hello"),
    ("nothing at all", ""),
    ("one colour", f"{ESC}[31mred{ESC}[0m"),
    ("colour with no reset", f"{ESC}[31mred"),
    ("two colours", f"{ESC}[31mred{ESC}[32mgreen{ESC}[0m"),
    ("bold", f"{ESC}[1mbold{ESC}[0m"),
    ("bold then not", f"{ESC}[1mbold{ESC}[22mplain"),
    ("colour and bold together", f"{ESC}[1;31mboth{ESC}[0m"),
    ("a bright colour", f"{ESC}[91mbright{ESC}[0m"),
    ("default colour", f"{ESC}[31mred{ESC}[39mplain"),
    (
        "every base colour",
        "".join(f"{ESC}[{code}m{name}" for code, name in zip(range(30, 38), SEGMENT_COLOR_NAMES, strict=True)),
    ),
    # Merging and empty runs.
    ("two escapes with no text between", f"{ESC}[31m{ESC}[32mgreen"),
    ("a reset that changes nothing", f"plain{ESC}[0mmore"),
    ("the same colour twice", f"{ESC}[31mred{ESC}[31mmore"),
    ("text after a trailing escape", f"text{ESC}[0m"),
    # Extended colour: operands skipped, not read.
    ("an extended 256 colour", f"{ESC}[38;5;196mtext{ESC}[0m"),
    ("an extended rgb colour", f"{ESC}[38;2;255;0;0mtext{ESC}[0m"),
    ("an extended colour then another code", f"{ESC}[38;5;196;1mtext"),
    ("an extended background", f"{ESC}[48;5;196mtext"),
    ("an extended colour with no operands", f"{ESC}[38mtext"),
    ("an extended colour after a colour", f"{ESC}[31m{ESC}[38;5;196mtext"),
    # Escapes that are not SGR.
    ("a cursor move", f"{ESC}[2Jcleared"),
    ("a cursor move between colours", f"{ESC}[31mred{ESC}[1;1Hmore"),
    ("a lone escape", f"{ESC}"),
    ("a lone escape before text", f"{ESC}text"),
    ("an escape with no parameters", f"{ESC}[mplain"),
    ("empty parameters between semicolons", f"{ESC}[;31mred"),
    # Codes outside the recognised set.
    ("an underline code", f"{ESC}[4munderlined"),
    ("a background colour", f"{ESC}[41mtext"),
    ("a code nobody uses", f"{ESC}[99mtext"),
]

# (name, tokens) — the dialect a caller writes, rendered the same way.
TOKEN_CASES: list[tuple[str, str]] = [
    ("a token colour", "{+g}green{-x}"),
    ("plain text", "no tokens here"),
    ("a token and a literal brace", "{+r}red{-x} and {{literal}}"),
    ("nothing at all", ""),
]


def _describe(segments: Any) -> list[dict[str, Any]]:
    """Segments as JSON carries them."""
    return [{"text": s.text, "color": s.color, "bold": s.bold} for s in segments]


def _build() -> dict[str, Any]:
    """Everything the segment parser decides."""
    return {
        "color_names": list(SEGMENT_COLOR_NAMES),
        "ansi": [
            {"name": name, "input": value, "segments": _describe(ansi_to_segments(value))} for name, value in ANSI_CASES
        ],
        "tokens": [
            {"name": name, "input": value, "segments": _describe(tokens_to_segments(value))}
            for name, value in TOKEN_CASES
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(ANSI_CASES)} ansi, {len(TOKEN_CASES)} token cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
