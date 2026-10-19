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
import unicodedata2
from wcwidth import wcwidth

from provide.uterm.emulator import _TolerantScreen

OUT = Path(__file__).with_name("vt_golden.json")

COLS = 20
ROWS = 6


def _capture(
    cols: int, rows: int, chunks: list[str], *, use_utf8: bool = True, screen_class: type[pyte.Screen] = pyte.Screen
) -> dict[str, Any]:
    """Feed chunks to a fresh screen and capture the full observable state."""
    screen = screen_class(cols, rows)
    stream = pyte.Stream(screen)
    stream.use_utf8 = use_utf8
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
    # ── Full reset (RIS) ───────────────────────────────────────────────────
    ("full reset clears the screen and homes the cursor", COLS, ROWS, ["abc\ndef\x1b[3;5H\x1bcx"]),
    ("full reset clears the pen", COLS, ROWS, ["\x1b[1;4;31;42m\x1bcx"]),
    ("full reset turns insert mode off", COLS, ROWS, ["\x1b[4h\x1bcabc\x1b[1;1HX"]),
    ("full reset shows the cursor", COLS, ROWS, ["\x1b[?25l\x1bc"]),
    ("full reset clears the scrolling region", COLS, ROWS, ["\x1b[2;4r\x1bca\x1b[4;1H\nb"]),
    ("full reset restores the tab stops", COLS, ROWS, ["\x1b[3g\x1bca\tb"]),
    ("full reset keeps the saved cursor", COLS, ROWS, ["\x1b[3;5H\x1b7\x1bc\x1b8x"]),
    ("full reset split across feeds", COLS, ROWS, ["abc\x1b", "cx"]),
    # ── Character sets in UTF-8 mode ───────────────────────────────────────
    ("designations are consumed but not applied", COLS, ROWS, ["\x1b(0q\x1b)0\x1b(Uq\x1b(Vq"]),
    ("shift out and shift in are ignored", COLS, ROWS, ["a\x0eb\x0fc\x0e"]),
    # ── Tab stops (HTS, TBC) ───────────────────────────────────────────────
    ("set a tab stop", COLS, ROWS, ["\x1b[1;4H\x1bH\ra\tb"]),
    ("clear the tab stop at the cursor", COLS, ROWS, ["\x1b[1;9H\x1b[g\ra\tb"]),
    ("clear the tab stop at the cursor with a zero", COLS, ROWS, ["\x1b[1;9H\x1b[0g\ra\tb"]),
    ("clear a column that has no tab stop", COLS, ROWS, ["\x1b[1;5H\x1b[g\ra\tb"]),
    ("clear every tab stop", COLS, ROWS, ["\x1b[3ga\tb"]),
    ("an unknown tab clear does nothing", COLS, ROWS, ["\x1b[1;9H\x1b[2g\ra\tb"]),
    ("a tab stop at the last column", COLS, ROWS, ["\x1b[3g\x1b[1;20H\x1bH\x1b[1;3H\tb"]),
    ("tab stops after clearing and setting", COLS, ROWS, ["\x1b[3g\x1b[1;6H\x1bH\x1b[1;12H\x1bH\r\ta\tb\tc"]),
    ("a tab from a pending wrap", COLS, ROWS, ["x" * COLS + "\ty"]),
    ("a tab stop set twice is cleared once", COLS, ROWS, ["\x1b[1;4H\x1bH\x1bH\x1b[g\ra\tb"]),
    ("a tab stop set at a pending wrap", COLS, ROWS, ["\x1b[3g" + "x" * COLS + "\x1bH\r\ty"]),
    # ── Autowrap (DECAWM) ──────────────────────────────────────────────────
    ("autowrap off overwrites the last column", COLS, ROWS, ["\x1b[?7l" + "x" * (COLS - 1) + "abc"]),
    ("autowrap off then on wraps again", COLS, ROWS, ["\x1b[?7l\x1b[?7h" + "x" * (COLS + 2)]),
    ("autowrap off keeps a pending wrap", COLS, ROWS, ["\x1b[?7l" + "x" * COLS + "\x1b[Dy"]),
    ("a control at a pending wrap still wraps", COLS, ROWS, ["x" * COLS + "\x01", "y"]),
    ("a control at a pending wrap with autowrap off", COLS, ROWS, ["\x1b[?7l" + "x" * COLS + "\x01", "y"]),
    ("a non-private mode this screen does not model", COLS, ROWS, ["\x1b[12habc\ndef\x1b[12l"]),
    # ── Origin mode (DECOM) and the scrolling region ───────────────────────
    ("origin mode homes to the top of the region", COLS, ROWS, ["\x1b[3;5r\x1b[?6hx"]),
    ("origin mode addresses rows within the region", COLS, ROWS, ["\x1b[3;5r\x1b[?6h\x1b[2;4Hx"]),
    ("origin mode ignores a position outside the region", COLS, ROWS, ["\x1b[3;5r\x1b[?6h\x1b[2;2H\x1b[9;9Hx"]),
    ("origin mode clamps the column", COLS, ROWS, ["\x1b[3;5r\x1b[?6h\x1b[1;99Hx"]),
    ("origin mode line position is relative", COLS, ROWS, ["\x1b[3;5r\x1b[?6h\x1b[2dx"]),
    ("origin mode line position clamps to the region", COLS, ROWS, ["\x1b[3;5r\x1b[?6h\x1b[9dx"]),
    ("origin mode off homes to the top of the screen", COLS, ROWS, ["\x1b[3;5r\x1b[?6h\x1b[?6lx"]),
    ("a region set in origin mode homes to its top", COLS, ROWS, ["\x1b[?6h\x1b[2;4rx"]),
    ("origin mode without a region is absolute", COLS, ROWS, ["\x1b[?6h\x1b[3;3Hx"]),
    ("origin mode keeps cursor movement in the region", COLS, ROWS, ["\x1b[2;4r\x1b[?6h\x1b[9B\x1b[9Cx"]),
    ("cursor up stops at the top of the region", COLS, ROWS, ["\x1b[2;5r\x1b[4;1H\x1b[9Ax"]),
    ("cursor up from above the region", COLS, ROWS, ["\x1b[3;5r\x1b[1;1H\x1b[Ax"]),
    ("cursor down stops at the bottom of the region", COLS, ROWS, ["\x1b[2;4r\x1b[2;1H\x1b[9Bx"]),
    ("cursor down from below the region", COLS, ROWS, ["\x1b[2;3r\x1b[6;1H\x1b[Bx"]),
    ("cursor next line stops at the bottom of the region", COLS, ROWS, ["\x1b[2;4r\x1b[2;5H\x1b[9Ex"]),
    ("cursor previous line stops at the top of the region", COLS, ROWS, ["\x1b[2;4r\x1b[4;5H\x1b[9Fx"]),
    ("line feed below the region", COLS, ROWS, ["\x1b[2;3r\x1b[6;1H\nx"]),
    ("reverse index above the region", COLS, ROWS, ["\x1b[3;5r\x1b[1;1H\x1bMx"]),
    ("resetting the region does not home", COLS, ROWS, ["\x1b[2;4r\x1b[5;5H\x1b[rx"]),
    ("a region with only a top", COLS, ROWS, ["ab\x1b[5;5H\x1b[3rc\x1b[3;1Hd\x1b[6;1H\ne"]),
    ("a region with only a bottom", COLS, ROWS, ["\x1b[5;5H\x1b[;3rx"]),
    # ── The savepoint stack (DECSC, DECRC) ─────────────────────────────────
    ("a second restore homes", COLS, ROWS, ["\x1b[3;5H\x1b7\x1b[1;1H\x1b8\x1b[1;9H\x1b8x"]),
    ("saves are restored last in first out", COLS, ROWS, ["\x1b[2;2H\x1b7\x1b[3;3H\x1b7\x1b[6;6H\x1b8a\x1b8b\x1b8c"]),
    ("restore clamps a pending wrap", COLS, ROWS, ["x" * COLS + "\x1b7\x1b8y"]),
    ("restore clamps to the region", COLS, ROWS, ["\x1b[6;1H\x1b7\x1b[2;4r\x1b8x"]),
    ("restore brings back origin mode", COLS, ROWS, ["\x1b[2;4r\x1b[?6h\x1b7\x1b[?6l\x1b8\x1b[1;1Hx"]),
    ("restore does not clear origin mode", COLS, ROWS, ["\x1b[2;4r\x1b7\x1b[?6h\x1b8\x1b[1;1Hx"]),
    ("restore with nothing saved clears origin mode", COLS, ROWS, ["\x1b[2;4r\x1b[?6h\x1b[3;3H\x1b8\x1b[1;1Hx"]),
    ("restore brings back autowrap", COLS, ROWS, ["\x1b7\x1b[?7l\x1b8" + "x" * (COLS + 1)]),
    ("restore does not clear autowrap", COLS, ROWS, ["\x1b[?7l\x1b7\x1b[?7h\x1b8" + "x" * (COLS + 1)]),
    ("restore brings back the pen and visibility", COLS, ROWS, ["\x1b[1;31m\x1b[?25l\x1b7\x1b[0m\x1b[?25h\x1b8x"]),
    # ── New line mode (LNM) ────────────────────────────────────────────────
    ("new line mode adds a carriage return", COLS, ROWS, ["\x1b[20habc\ndef"]),
    ("new line mode off", COLS, ROWS, ["\x1b[20h\x1b[20labc\ndef"]),
    ("new line mode for vertical tab and form feed", COLS, ROWS, ["\x1b[20hab\x0bc\x0cd"]),
    ("new line mode leaves index alone", COLS, ROWS, ["\x1b[20habc\x1bDx"]),
    ("new line mode applies to next line", COLS, ROWS, ["\x1b[20habc\x1bEx"]),
    ("new line mode on a wrap", COLS, ROWS, ["\x1b[20h" + "x" * (COLS + 1)]),
    ("full reset turns new line mode off", COLS, ROWS, ["\x1b[20h\x1bcab\ncd"]),
    ("full reset turns origin mode off", COLS, ROWS, ["\x1b[2;4r\x1b[?6h\x1bc\x1b[1;1Hx"]),
    ("full reset turns autowrap back on", COLS, ROWS, ["\x1b[?7l\x1bc" + "x" * (COLS + 1)]),
    # ── C1 controls ────────────────────────────────────────────────────────
    ("c1 csi", COLS, ROWS, ["ab\x9b2Dx"]),
    ("c1 csi with a private mode", COLS, ROWS, ["\x9b?25lx"]),
    ("c1 csi split across feeds", COLS, ROWS, ["ab\x9b", "2Dx"]),
    ("c1 osc terminated by bel", COLS, ROWS, ["a\x9d0;t\x07b"]),
    ("c1 osc terminated by c1 st", COLS, ROWS, ["a\x9d0;t\x9cb"]),
    ("c1 osc terminated by c0 st", COLS, ROWS, ["a\x9d0;t\x1b\\b"]),
    ("c0 osc terminated by c1 st", COLS, ROWS, ["a\x1b]0;t\x9cb"]),
    ("an escape inside an osc does not end it", COLS, ROWS, ["a\x1b]0;t\x1bxy\x07b"]),
    ("an osc palette reset ends at once", COLS, ROWS, ["a\x1b]Rbc"]),
    ("a c1 osc palette set ends at once", COLS, ROWS, ["a\x9dPbc"]),
    ("an osc whose code is bel", COLS, ROWS, ["a\x1b]\x07bc\x07d"]),
    # ── Character width (wcwidth) ──────────────────────────────────────────
    ("a wide character takes two cells", COLS, ROWS, ["\u4e2dx"]),
    ("an emoji takes two cells", COLS, ROWS, ["\U0001f600x"]),
    ("a fullwidth form takes two cells", COLS, ROWS, ["\uff21x"]),
    ("a wide character with a pen", COLS, ROWS, ["\x1b[1;31m\u4e2d\x1b[0mx"]),
    ("a wide character in the last column", COLS, ROWS, ["x" * (COLS - 1) + "\u4e2d"]),
    ("a wide character in the last column then text", COLS, ROWS, ["x" * (COLS - 1) + "\u4e2dy"]),
    ("a wide character at a pending wrap", COLS, ROWS, ["x" * COLS + "\u4e2dy"]),
    ("a wide character at a pending wrap with autowrap off", COLS, ROWS, ["\x1b[?7l" + "x" * COLS + "\u4e2d"]),
    ("a wide character over the second half of another", COLS, ROWS, ["\u4e2d\x1b[1;2Hab"]),
    ("a wide character in insert mode", COLS, ROWS, ["abcd\x1b[1;1H\x1b[4h\u4e2d"]),
    ("cursor back from a wide character in the last column", COLS, ROWS, ["x" * (COLS - 1) + "\u4e2d\x1b[Dy"]),
    ("a combining mark joins the cell before it", COLS, ROWS, ["e\u0301x"]),
    ("combining marks that compose and one that does not", COLS, ROWS, ["a\u0301\u0302q\u0307"]),
    ("a combining mark after a pending wrap joins the row above", COLS, ROWS, ["x" * COLS + "\u0301y"]),
    ("a combining mark at the top left is dropped", COLS, ROWS, ["\u0301x"]),
    ("a combining mark at column zero joins the row above", COLS, ROWS, ["abc\r\n\u0301x"]),
    ("a combining mark after a wide character joins its second cell", COLS, ROWS, ["\u4e2d\u0301x"]),
    ("a zero width space ends the run", COLS, ROWS, ["a\u200bb", "c"]),
    ("a zero width joiner ends the run", COLS, ROWS, ["\U0001f468\u200d\U0001f469x"]),
    ("a variation selector ends the run", COLS, ROWS, ["\u2764\ufe0fx"]),
    ("a hangul medial vowel ends the run", COLS, ROWS, ["\u1100\u1161x"]),
    ("ambiguous width is narrow", COLS, ROWS, ["\u00b1\u00e9\u2500x"]),
    # U+105C9 is a Unicode 16 composite of these two. The pinned data (18.0)
    # composes them; CPython's own unicodedata before 3.14 would not.
    ("a unicode 16 composite composes", COLS, ROWS, ["\U000105d2\u0307x"]),
    ("marks composed out of canonical order", COLS, ROWS, ["a\u0323\u0302q\u0302\u0323"]),
    ("hangul jamo drawn as marks stay apart", COLS, ROWS, ["\u1100\u1161"]),
]


# Streams with a C0 control pyte has no handler for. It belongs to the run of
# text it arrived in, it has no width, and pyte stops drawing the run there:
# the rest of that run is dropped, though the stream is parsed on as before.
# The Go and C# ports do the same, and so does this one. They are kept under
# "stall_divergences", the name they were recorded under when this port drew
# the control instead, so that the corpus only ever grows.
STALL_CASES: list[tuple[str, list[str]]] = [
    ("unhandled c0 swallows the rest of the stream", ["a\x01bc"]),
    ("a second unhandled c0 changes nothing", ["a\x01\x01bc"]),
    ("unhandled c0 mid-line", ["ab\x05cd"]),
]


# Streams fed with pyte's ``use_utf8`` off, the mode the Python emulator runs
# a UTF-8 session in so that ESC ( 0, ESC ) 0 and SI/SO draw DEC special
# graphics. The TypeScript stream reproduces them with ``decGraphics`` on.
DEC_GRAPHICS_CASES: list[tuple[str, int, int, list[str]]] = [
    ("dec graphics in a utf-8 line", 30, ROWS, ["\x1b(0lqqk\x1b(B café ─│ \x0eqq\x0f x"]),
    ("a box", COLS, ROWS, ["\x1b(0lqqk\r\nx  x\r\nmqqj\x1b(B ok"]),
    ("every mapped character", COLS, ROWS, ["\x1b(0+,-.0_`abcdefghijklmnopqrstuvwxyz{|}~"]),
    ("characters the set leaves alone", COLS, ROWS, ["\x1b(0AZ19/^\x7f"]),
    ("g1 designated and shifted", COLS, ROWS, ["\x1b)0ab\x0elqk\x0fcd"]),
    ("g1 back to ascii", COLS, ROWS, ["\x1b)0\x1b)B\x0elqk"]),
    ("shift out with g1 untouched", COLS, ROWS, ["ab\x0eqq\x0fcd"]),
    ("designation split across feeds", COLS, ROWS, ["\x1b(", "0q", "\x1b", "(Bq"]),
    ("unknown designator is consumed", COLS, ROWS, ["\x1b(Zabc"]),
    ("save and restore keep the charset", COLS, ROWS, ["\x1b(0\x1b7\x1b(B\x1b8lqk"]),
    ("restore with nothing saved keeps the charset", COLS, ROWS, ["\x1b(0\x1b8lqk"]),
    ("styled graphics keep their attributes", COLS, ROWS, ["\x1b[1;32m\x1b(0qq\x1b(B\x1b[0mq"]),
    # 0x9B and above in the C1 range are controls to the reference (CSI, ST,
    # OSC), so the 0x80 row stops short of them.
    ("ibmpc from 0x80 to 0x9a", COLS, ROWS, ["\x1b(U" + "".join(map(chr, range(0x80, 0x9B)))]),
    ("ibmpc for the c1 bytes that are not controls", COLS, ROWS, ["\x1b(U\x9c\x9e\x9f"]),
    ("ibmpc draws the c0 bytes that are text", COLS, ROWS, ["\x1b(U\x01\x02\x1f\x10"]),
    ("a control ends the run of text in ascii", COLS, ROWS, ["\x1b(Ua\x01b\x1b(Bc\x01d", "e"]),
    ("ibmpc above 0x9f", COLS, ROWS, ["\x1b(U" + "".join(map(chr, range(0xA0, 0x100)))]),
    ("ibmpc leaves printable ascii alone", COLS, ROWS, ["\x1b(U !09AZaz~\x7f"]),
    ("ibmpc in g1 shifted", COLS, ROWS, ["\x1b)U\x0e\xcd\xba\xb0\x0f\xcd"]),
    ("ibmpc leaves code points above 255 alone", COLS, ROWS, ["\x1b(U\u2500\u00e9\u20ac"]),
    ("vax42 below 0x80", COLS, ROWS, ["\x1b(V" + "".join(map(chr, range(0x20, 0x7F)))]),
    ("vax42 above 0x9f", COLS, ROWS, ["\x1b(V" + "".join(map(chr, range(0xA0, 0x100)))]),
    ("full reset returns the sets to ascii and dec", COLS, ROWS, ["\x1b(0\x1b)U\x0e\x1bcqx\x0eq"]),
    ("full reset keeps the saved sets", COLS, ROWS, ["\x1b(U\x1b7\x1bc\xcd\x1b8\xcd"]),
]


# Streams plain pyte raises on, recorded through the screen the Python
# emulator runs (provide.uterm.emulator._TolerantScreen), which fixes both: a
# line position in origin mode with no region is absolute and clamped, and the
# empty second cell of a wide character whose first cell was overwritten is
# displayed as nothing. Every port reproduces these.
REFERENCE_FIX_CASES: list[tuple[str, int, int, list[str]]] = [
    ("half of an overwritten wide character", COLS, ROWS, ["\u4e2d\x1b[1;1Ha"]),
    ("a narrow character over the first half of a wide one", COLS, ROWS, ["\u4e2d\u4e2d\x1b[1;3Hx"]),
    ("a line position in origin mode with no region", COLS, ROWS, ["\x1b[?6h\x1b[1;5H\x1b[3dx"]),
    ("a line position past the screen in origin mode with no region", COLS, ROWS, ["\x1b[?6h\x1b[9dx"]),
]


def _width_ranges() -> list[list[int]]:
    """Every run of code points whose wcwidth is not 1, as [first, last, width]."""
    ranges: list[list[int]] = []
    for point in range(0x110000):
        width = wcwidth(chr(point))
        if width == 1:
            continue
        if ranges and ranges[-1][1] == point - 1 and ranges[-1][2] == width:
            ranges[-1][1] = point
        else:
            ranges.append([point, point, width])
    return ranges


def _combining_classes() -> list[list[int]]:
    """Every run of code points with one non-zero combining class, as [first, last, class]."""
    ranges: list[list[int]] = []
    for point in range(0x110000):
        combining = unicodedata2.combining(chr(point))
        if not combining:
            continue
        if ranges and ranges[-1][1] == point - 1 and ranges[-1][2] == combining:
            ranges[-1][1] = point
        else:
            ranges.append([point, point, combining])
    return ranges


def _combining_ranges() -> list[list[int]]:
    """Every run of code points with a non-zero combining class, as [first, last]."""
    ranges: list[list[int]] = []
    for first, last, _ in _combining_classes():
        if ranges and ranges[-1][1] == first - 1:
            ranges[-1][1] = last
        else:
            ranges.append([first, last])
    return ranges


def _canonical_decompositions() -> list[list[int]]:
    """Each canonical one-level decomposition, as [code point, first, second or -1]."""
    out = []
    for point in range(0x110000):
        # Hangul syllables decompose by arithmetic in every port, not by table.
        if 0xAC00 <= point <= 0xD7A3:
            continue
        mapping = unicodedata2.decomposition(chr(point))
        if mapping and not mapping.startswith("<"):
            parts = [int(part, 16) for part in mapping.split()]
            out.append([point, parts[0], parts[1] if len(parts) > 1 else -1])
    return out


def _composition_pairs() -> list[list[int]]:
    """Each primary composite, as [starter, combiner, composite]: what NFC recomposes."""
    return sorted(
        [first, second, point]
        for point, first, second in _canonical_decompositions()
        if second >= 0 and unicodedata2.normalize("NFC", chr(first) + chr(second)) == chr(point)
    )


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
        # The tables ESC ( U and ESC ( V draw from, as code points, for the
        # same reason: the port's copies are checked entry by entry against
        # these, including the C0 entries no stream can reach.
        "charset_tables": {code: [ord(c) for c in pyte.charsets.MAPS[code]] for code in ("U", "V")},
        "cases": records,
        "dec_graphics_cases": [
            {
                "name": name,
                "cols": cols,
                "rows": rows,
                "chunks": chunks,
                "state": _capture(cols, rows, chunks, use_utf8=False),
            }
            for (name, cols, rows, chunks) in DEC_GRAPHICS_CASES
        ],
        "stall_divergences": [
            {"name": name, "chunks": chunks, "pyte": _capture(COLS, ROWS, chunks)} for (name, chunks) in STALL_CASES
        ],
        "reference_fix_cases": [
            {
                "name": name,
                "cols": cols,
                "rows": rows,
                "chunks": chunks,
                "state": _capture(cols, rows, chunks, screen_class=_TolerantScreen),
            }
            for (name, cols, rows, chunks) in REFERENCE_FIX_CASES
        ],
        # The width pyte gives each code point (wcwidth) and the Unicode data
        # it composes a combining mark with (unicodedata2, pinned: see
        # provide/uterm/_unicode.py), exported for the same reason as
        # fg_bg_256: every port's tables are checked against these, so a
        # wcwidth or Unicode upgrade shows up as a failing port until
        # scripts/gen_unicode_tables.py is rerun.
        "wcwidth_version": version("wcwidth"),
        "unicode_version": unicodedata2.unidata_version,
        "wcwidth_ranges": _width_ranges(),
        "combining_ranges": _combining_ranges(),
        "combining_classes": _combining_classes(),
        "canonical_decompositions": _canonical_decompositions(),
        "composition_pairs": _composition_pairs(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(records)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
