#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``vt`` port.

``vt`` reproduces the observable behaviour of pyte, which the Python
implementation depends on for terminal emulation. pyte is LGPL, so nothing is
copied from it: this corpus records what it *does* for a set of inputs, and
the port is written against that record.

Each case feeds a byte stream to a fresh screen and captures the full
observable state — every display line, the cursor, and the attributes of
every cell that differs from the default. Recording full state rather than
just the display text is what makes a colour or attribute regression fail
loudly instead of silently.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_vt_golden.py
"""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pyte

OUT = Path(__file__).with_name("vt_golden.json")

COLS = 20
ROWS = 6


def _capture(cols: int, rows: int, chunks: list[str]) -> dict[str, Any]:
    """Feed chunks to a fresh screen and capture the full observable state."""
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    for chunk in chunks:
        stream.feed(chunk)

    # Only cells that differ from the default are recorded, so the corpus
    # stays readable and a diff points at the cell that actually moved.
    default = pyte.screens.Char(" ")
    cells: list[dict[str, Any]] = []
    for y in range(rows):
        row = screen.buffer.get(y, {})
        for x in range(cols):
            char = row.get(x)
            if char is None:
                continue
            if (
                char.data == default.data
                and char.fg == default.fg
                and char.bg == default.bg
                and char.bold == default.bold
                and char.italics == default.italics
                and char.underscore == default.underscore
                and char.reverse == default.reverse
                and char.strikethrough == default.strikethrough
                and char.blink == default.blink
            ):
                continue
            cells.append(
                {
                    "y": y,
                    "x": x,
                    "data": char.data,
                    "fg": char.fg,
                    "bg": char.bg,
                    "bold": char.bold,
                    "italics": char.italics,
                    "underscore": char.underscore,
                    "reverse": char.reverse,
                    "strikethrough": char.strikethrough,
                    "blink": char.blink,
                }
            )

    return {
        "display": list(screen.display),
        "cursor": {"x": screen.cursor.x, "y": screen.cursor.y, "hidden": screen.cursor.hidden},
        "cells": cells,
    }


# (name, cols, rows, chunks). Chunks are fed in order to one screen, so a case
# can prove that state survives a feed boundary.
CASES: list[tuple[str, int, int, list[str]]] = [
    # ── Plain text and wrapping ────────────────────────────────────────────
    ("empty stream", COLS, ROWS, []),
    ("plain text", COLS, ROWS, ["hello"]),
    ("text split across feeds", COLS, ROWS, ["hel", "lo"]),
    ("exactly one full row", COLS, ROWS, ["x" * COLS]),
    ("wrap onto the next row", COLS, ROWS, ["x" * (COLS + 3)]),
    ("wrap at the last row scrolls", COLS, ROWS, ["x" * (COLS * ROWS + 1)]),
    ("non-ascii text", COLS, ROWS, ["café ▒▓"]),
    # ── C0 controls ────────────────────────────────────────────────────────
    ("carriage return", COLS, ROWS, ["abc\rX"]),
    ("line feed", COLS, ROWS, ["abc\ndef"]),
    ("crlf", COLS, ROWS, ["abc\r\ndef"]),
    ("backspace", COLS, ROWS, ["abc\bX"]),
    ("backspace at column zero", COLS, ROWS, ["\bX"]),
    ("tab", COLS, ROWS, ["a\tb"]),
    ("tab past the last stop", COLS, ROWS, ["x" * 18 + "\ta"]),
    ("bell is not printed", COLS, ROWS, ["a\x07b"]),
    ("line feed at the last row scrolls", COLS, ROWS, ["\n" * (ROWS + 1) + "x"]),
    # ── Cursor movement ────────────────────────────────────────────────────
    ("cursor up", COLS, ROWS, ["\n\n\x1b[Ax"]),
    ("cursor up at the top edge", COLS, ROWS, ["\x1b[Ax"]),
    ("cursor down", COLS, ROWS, ["\x1b[Bx"]),
    ("cursor forward", COLS, ROWS, ["\x1b[3Cx"]),
    ("cursor back", COLS, ROWS, ["abcd\x1b[2Dx"]),
    ("cursor position", COLS, ROWS, ["\x1b[3;5Hx"]),
    ("cursor position with no parameters homes", COLS, ROWS, ["abc\x1b[Hx"]),
    ("cursor position clamps past the edge", COLS, ROWS, ["\x1b[99;99Hx"]),
    ("cursor column", COLS, ROWS, ["abc\x1b[1Gx"]),
    ("cursor line", COLS, ROWS, ["\x1b[3dx"]),
    ("save and restore cursor", COLS, ROWS, ["\x1b[3;5H\x1b7\x1b[1;1H\x1b8x"]),
    ("index moves down", COLS, ROWS, ["\x1bDx"]),
    ("reverse index moves up", COLS, ROWS, ["\n\n\x1bMx"]),
    ("next line", COLS, ROWS, ["abc\x1bEx"]),
    # ── Erasing ────────────────────────────────────────────────────────────
    ("erase to end of line", COLS, ROWS, ["abcdef\x1b[3G\x1b[K"]),
    ("erase to start of line", COLS, ROWS, ["abcdef\x1b[3G\x1b[1K"]),
    ("erase whole line", COLS, ROWS, ["abcdef\x1b[2K"]),
    ("erase to end of screen", COLS, ROWS, ["ab\ncd\nef\x1b[2;1H\x1b[J"]),
    ("erase to start of screen", COLS, ROWS, ["ab\ncd\nef\x1b[2;2H\x1b[1J"]),
    ("erase whole screen", COLS, ROWS, ["ab\ncd\x1b[2J"]),
    ("erase characters", COLS, ROWS, ["abcdef\x1b[1G\x1b[3X"]),
    # ── Insert and delete ──────────────────────────────────────────────────
    ("insert lines", COLS, ROWS, ["ab\ncd\x1b[1;1H\x1b[2L"]),
    ("delete lines", COLS, ROWS, ["ab\ncd\nef\x1b[1;1H\x1b[1M"]),
    ("insert characters", COLS, ROWS, ["abcdef\x1b[3G\x1b[2@"]),
    ("delete characters", COLS, ROWS, ["abcdef\x1b[3G\x1b[2P"]),
    # ── Scrolling regions ──────────────────────────────────────────────────
    ("set scrolling region", COLS, ROWS, ["\x1b[2;4r\x1b[2;1H" + "a\nb\nc\nd"]),
    ("reset scrolling region", COLS, ROWS, ["\x1b[2;4r\x1b[r\x1b[1;1Hx"]),
    # ── Graphic rendition ──────────────────────────────────────────────────
    ("reset attributes", COLS, ROWS, ["\x1b[0ma"]),
    ("bold", COLS, ROWS, ["\x1b[1ma"]),
    ("italics", COLS, ROWS, ["\x1b[3ma"]),
    ("underscore", COLS, ROWS, ["\x1b[4ma"]),
    ("blink", COLS, ROWS, ["\x1b[5ma"]),
    ("reverse", COLS, ROWS, ["\x1b[7ma"]),
    ("strikethrough", COLS, ROWS, ["\x1b[9ma"]),
    ("foreground colours", COLS, ROWS, ["".join(f"\x1b[{30 + i}ma" for i in range(8))]),
    ("background colours", COLS, ROWS, ["".join(f"\x1b[{40 + i}mb" for i in range(8))]),
    ("bright foreground colours", COLS, ROWS, ["".join(f"\x1b[{90 + i}ma" for i in range(8))]),
    ("default foreground and background", COLS, ROWS, ["\x1b[31;42ma\x1b[39;49mb"]),
    ("256-colour foreground", COLS, ROWS, ["\x1b[38;5;196ma"]),
    ("256-colour background", COLS, ROWS, ["\x1b[48;5;21ma"]),
    ("truecolor foreground", COLS, ROWS, ["\x1b[38;2;255;0;0ma"]),
    ("attributes reset by zero", COLS, ROWS, ["\x1b[1;31ma\x1b[0mb"]),
    ("attributes persist across feeds", COLS, ROWS, ["\x1b[1;31m", "ab"]),
    ("empty parameter list resets", COLS, ROWS, ["\x1b[1ma\x1b[mb"]),
    # ── Modes ──────────────────────────────────────────────────────────────
    ("hide cursor", COLS, ROWS, ["\x1b[?25l"]),
    ("show cursor", COLS, ROWS, ["\x1b[?25l\x1b[?25h"]),
    ("insert mode", COLS, ROWS, ["abcdef\x1b[3G\x1b[4hXY"]),
    ("insert mode off", COLS, ROWS, ["abcdef\x1b[3G\x1b[4h\x1b[4lXY"]),
    # ── Malformed and unsupported input ────────────────────────────────────
    ("unterminated escape", COLS, ROWS, ["a\x1b"]),
    ("unterminated csi", COLS, ROWS, ["a\x1b[1"]),
    ("unknown final byte", COLS, ROWS, ["a\x1b[1!b"]),
    ("unknown escape", COLS, ROWS, ["a\x1bZb"]),
    ("csi split across feeds", COLS, ROWS, ["ab\x1b[", "2Dx"]),
    ("escape split across feeds", COLS, ROWS, ["ab\x1b", "[2Dx"]),
    ("osc is swallowed", COLS, ROWS, ["a\x1b]0;title\x07b"]),
    ("very large parameter", COLS, ROWS, ["\x1b[999999Cx"]),
    ("many parameters", COLS, ROWS, ["\x1b[1;2;3;4;5;6;7;8mx"]),
    ("empty parameter among others", COLS, ROWS, ["\x1b[;5Hx"]),
    ("delete control is ignored", COLS, ROWS, ["a\x7fb"]),
    ("null control is ignored", COLS, ROWS, ["a\x00b"]),
    ("osc terminated by st", COLS, ROWS, ["a\x1b]0;t\x1b\\b"]),
    # ── Paths reachable only from an untouched row or an edge ──────────────
    ("reverse index at the top scrolls", COLS, ROWS, ["ab\x1b[1;1H\x1bMx"]),
    ("restore cursor with nothing saved", COLS, ROWS, ["\x1b[3;5H\x1b8x"]),
    ("insert lines above the scrolling region", COLS, ROWS, ["ab\ncd\x1b[3;5r\x1b[1;1H\x1b[2Lx"]),
    ("delete lines above the scrolling region", COLS, ROWS, ["ab\ncd\x1b[3;5r\x1b[1;1H\x1b[2Mx"]),
    ("insert characters on an untouched row", COLS, ROWS, ["\x1b[3;1H\x1b[2@x"]),
    ("delete characters on an untouched row", COLS, ROWS, ["\x1b[3;1H\x1b[2Px"]),
    ("unknown non-private mode set", COLS, ROWS, ["\x1b[20hx"]),
    ("unknown non-private mode reset", COLS, ROWS, ["\x1b[20lx"]),
    ("extended colour with an unknown mode", COLS, ROWS, ["\x1b[38;9;1ma"]),
    ("extended colour truncated", COLS, ROWS, ["\x1b[38;5ma"]),
    ("extended background truncated", COLS, ROWS, ["\x1b[48;2;1ma"]),
    ("256-colour index out of range", COLS, ROWS, ["\x1b[38;5;999ma"]),
    ("margins that are too narrow are ignored", COLS, ROWS, ["\x1b[3;3r\x1b[1;1Hx"]),
]


# Streams where pyte's own behaviour is not worth reproducing. A C0 control
# it has no handler for stalls its parser: everything after the control is
# swallowed for the life of the stream, so one stray byte freezes the display
# permanently. The Go port draws such a control instead, and this port follows
# Go — the ports are meant to agree with each other, and a stall is a bug.
# Recorded so the choice is visible rather than assumed.
STALL_CASES: list[tuple[str, list[str]]] = [
    ("unhandled c0 swallows the rest of the stream", ["a\x01bc"]),
    ("a second unhandled c0 changes nothing", ["a\x01\x01bc"]),
    ("unhandled c0 mid-line", ["ab\x05cd"]),
]


def main() -> int:
    """Write the golden corpus and report the record count."""
    records = [
        {"name": name, "cols": cols, "rows": rows, "chunks": chunks, "state": _capture(cols, rows, chunks)}
        for (name, cols, rows, chunks) in CASES
    ]
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_vt_golden.py",
        "pyte_version": version("pyte"),
        # The 256-colour to hex table pyte resolves SGR 38;5;N / 48;5;N
        # against. Exported rather than transcribed: its first sixteen
        # entries are pyte's own base palette, not the uterm BBS one.
        "fg_bg_256": list(pyte.graphics.FG_BG_256),
        "cases": records,
        "stall_divergences": [
            {"name": name, "chunks": chunks, "pyte": _capture(COLS, ROWS, chunks)} for (name, chunks) in STALL_CASES
        ],
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(records)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
