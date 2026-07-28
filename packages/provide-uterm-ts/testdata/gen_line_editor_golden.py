#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``lineEditor`` port.

Each case drives a fresh editor through a character sequence and records, per
character, exactly what the editor wrote to the terminal and what its state
became. The escape sequences are the contract — a port that gets the buffer
right but the cursor arithmetic wrong would corrupt the user's line.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_line_editor_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.line_editor import LineEditor

OUT = Path(__file__).with_name("line_editor_golden.json")

# Control characters the editor recognises, named for readable case lists.
ENTER = "\r"
NEWLINE = "\n"
BACKSPACE = "\x7f"
BACKSPACE_ALT = "\x08"
CTRL_A = "\x01"
CTRL_B = "\x02"
CTRL_E = "\x05"
CTRL_F = "\x06"
CTRL_K = "\x0b"
CTRL_U = "\x15"
CTRL_W = "\x17"

# (name, max_length, password_mode, characters)
CASES: list[tuple[str, int, bool, list[str]]] = [
    ("plain typing then enter", 80, False, [*"abc", ENTER]),
    ("newline also completes the line", 80, False, [*"ab", NEWLINE]),
    ("enter on an empty buffer", 80, False, [ENTER]),
    ("two lines in sequence", 80, False, [*"one", ENTER, *"two", ENTER]),
    # Password masking affects echo but not the buffer.
    ("password masking on insert", 80, True, [*"secret", ENTER]),
    # Backspace at the end and in the middle.
    ("backspace at end of line", 80, False, [*"abc", BACKSPACE]),
    ("backspace alternate code", 80, False, [*"abc", BACKSPACE_ALT]),
    ("backspace on an empty buffer is a no-op", 80, False, [BACKSPACE]),
    ("backspace mid-line redraws the tail", 80, False, [*"abcd", CTRL_A, CTRL_F, CTRL_F, BACKSPACE]),
    ("backspace mid-line in password mode", 80, True, [*"abcd", CTRL_A, CTRL_F, CTRL_F, BACKSPACE]),
    # Cursor movement.
    ("ctrl-a from mid-line", 80, False, [*"abc", CTRL_A]),
    ("ctrl-a at start is a no-op", 80, False, [CTRL_A]),
    ("ctrl-e from start", 80, False, [*"abc", CTRL_A, CTRL_E]),
    ("ctrl-e at end is a no-op", 80, False, [*"abc", CTRL_E]),
    ("ctrl-b moves left", 80, False, [*"abc", CTRL_B]),
    ("ctrl-b at start is a no-op", 80, False, [CTRL_B]),
    ("ctrl-f moves right", 80, False, [*"abc", CTRL_A, CTRL_F]),
    ("ctrl-f at end is a no-op", 80, False, [*"abc", CTRL_F]),
    # Kill operations.
    ("ctrl-u from end of line", 80, False, [*"abc", CTRL_U]),
    ("ctrl-u mid-line keeps the tail", 80, False, [*"abcd", CTRL_B, CTRL_B, CTRL_U]),
    ("ctrl-u at start is a no-op", 80, False, [CTRL_U]),
    ("ctrl-u mid-line in password mode", 80, True, [*"abcd", CTRL_B, CTRL_B, CTRL_U]),
    ("ctrl-k from mid-line", 80, False, [*"abcd", CTRL_B, CTRL_B, CTRL_K]),
    ("ctrl-k at end is a no-op", 80, False, [*"abc", CTRL_K]),
    # Word kill: trailing spaces, then the word.
    ("ctrl-w on a single word", 80, False, [*"hello", CTRL_W]),
    ("ctrl-w skips trailing spaces first", 80, False, [*"one two   ", CTRL_W]),
    ("ctrl-w on the second of two words", 80, False, [*"one two", CTRL_W]),
    ("ctrl-w with only spaces", 80, False, [*"   ", CTRL_W]),
    ("ctrl-w at start is a no-op", 80, False, [CTRL_W]),
    ("ctrl-w mid-line keeps the tail", 80, False, [*"one two three", CTRL_B, CTRL_B, CTRL_B, CTRL_B, CTRL_B, CTRL_W]),
    ("ctrl-w mid-line in password mode", 80, True, [*"one two", CTRL_B, CTRL_B, CTRL_W]),
    # Insertion limits.
    ("insertion at the length limit rings the bell", 3, False, [*"abcd"]),
    ("limit of one character", 1, False, [*"ab"]),
    # Mid-line insertion redraw.
    ("mid-line insertion redraws the tail", 80, False, [*"abc", CTRL_A, *"X"]),
    ("mid-line insertion in password mode", 80, True, [*"abc", CTRL_A, *"X"]),
    # Non-ASCII and unrecognised controls fall through to insertion.
    ("non-ascii character inserts", 80, False, [*"aéb", ENTER]),
    ("unrecognised control inserts literally", 80, False, ["\x1f", ENTER]),
    # A long interleaved edit session.
    (
        "interleaved editing session",
        80,
        False,
        [*"hello world", CTRL_A, *"say ", CTRL_E, *"!", CTRL_W, CTRL_U, *"done", ENTER],
    ),
]


async def _run_case(max_length: int, password_mode: bool, chars: list[str]) -> list[dict[str, Any]]:
    """Drive one editor and record per-character output and state."""
    writes: list[str] = []

    async def on_write(data: str) -> None:
        writes.append(data)

    editor = LineEditor(max_length=max_length, password_mode=password_mode, on_write=on_write)
    steps: list[dict[str, Any]] = []
    for ch in chars:
        writes.clear()
        line = await editor.process_char(ch)
        steps.append(
            {
                "char": ch,
                "emitted": "".join(writes),
                "line": line,
                "buffer": editor.buffer,
                "cursor": editor.cursor_pos,
            }
        )
    return steps


async def _silent_mode() -> dict[str, Any]:
    """An editor with no write callback must still track state."""
    editor = LineEditor(max_length=80)
    for ch in [*"abc", "\x7f"]:
        await editor.process_char(ch)
    return {"buffer": editor.buffer, "cursor": editor.cursor_pos}


async def _run() -> dict[str, Any]:
    """Build every section of the corpus."""
    cases = []
    for name, max_length, password_mode, chars in CASES:
        cases.append(
            {
                "name": name,
                "max_length": max_length,
                "password_mode": password_mode,
                "chars": chars,
                "steps": await _run_case(max_length, password_mode, chars),
            }
        )
    return {"cases": cases, "silent_mode": await _silent_mode()}


def main() -> int:
    """Write the golden corpus and report the record count."""
    sections = asyncio.run(_run())
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_line_editor_golden.py",
        **sections,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    steps = sum(len(case["steps"]) for case in sections["cases"])
    print(f"wrote {OUT} ({len(sections['cases'])} cases, {steps} steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
