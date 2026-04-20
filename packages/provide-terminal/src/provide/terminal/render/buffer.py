#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI SGR rendering utilities for pyte-backed terminal emulation.

Provides :class:`AnsiBuffer` — a thin wrapper around a pyte ``Screen`` and
``Stream`` that accepts raw bytes, feeds them through the terminal emulator,
and re-renders the virtual screen as a list of ANSI-styled strings suitable
for output to a real terminal.

Requires the ``emulator`` optional extra
(``pip install 'provide-terminal[emulator]'``).
"""

from __future__ import annotations

from typing import Any

import pyte

# ---------------------------------------------------------------------------
# ANSI escape constants
# ---------------------------------------------------------------------------

ANSI_RESET = "\x1b[0m"
ANSI_HIDE_CURSOR = "\x1b[?25l"
ANSI_SHOW_CURSOR = "\x1b[?25h"
ANSI_ALT_SCREEN = "\x1b[?1049h"
ANSI_EXIT_ALT = "\x1b[?1049l"


def move_to(row: int, col: int) -> str:
    """Return the CSI sequence to position the cursor at *row*, *col* (1-based)."""
    return f"\x1b[{row};{col}H"


def clear_screen() -> str:
    """Return the CSI sequence to erase the entire screen."""
    return "\x1b[2J"


# ---------------------------------------------------------------------------
# 16-color name -> SGR code mappings
# ---------------------------------------------------------------------------

FG_CODES: dict[str, int] = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "brightblack": 90,
    "brightred": 91,
    "brightgreen": 92,
    "brightyellow": 93,
    "brightblue": 94,
    "brightmagenta": 95,
    "brightcyan": 96,
    "brightwhite": 97,
}

BG_CODES: dict[str, int] = {
    "black": 40,
    "red": 41,
    "green": 42,
    "yellow": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
    "brightblack": 100,
    "brightred": 101,
    "brightgreen": 102,
    "brightyellow": 103,
    "brightblue": 104,
    "brightmagenta": 105,
    "brightcyan": 106,
    "brightwhite": 107,
}


# ---------------------------------------------------------------------------
# AnsiBuffer — pyte Screen + Stream wrapper
# ---------------------------------------------------------------------------


class AnsiBuffer:
    """Virtual terminal backed by a pyte ``Screen``.

    Feed raw bytes with :meth:`feed` and retrieve ANSI-styled output lines
    with :meth:`render_lines`.
    """

    def __init__(self, cols: int, rows: int) -> None:
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)

    def resize(self, cols: int, rows: int) -> None:
        self._screen.resize(rows, cols)

    def reset(self) -> None:
        self._screen.reset()

    def feed(self, data: bytes) -> None:
        if not data:
            return
        text = data.decode("cp437", errors="replace")
        self._stream.feed(text)

    def render_lines(self, width: int, height: int) -> list[str]:
        lines: list[str] = []
        buffer = self._screen.buffer
        for y in range(height):
            row: dict[int, Any] = buffer.get(y, {})
            line_parts: list[str] = []
            last_style: tuple[str, str, bool, bool, bool, bool] | None = None
            for x in range(width):
                cell = row.get(x)
                if cell is None:
                    char = " "
                    style = ("default", "default", False, False, False, False)
                else:
                    fg = cell.fg or "default"
                    bg = cell.bg or "default"
                    bold = bool(cell.bold)
                    underscore = bool(getattr(cell, "underscore", False))
                    reverse = bool(getattr(cell, "reverse", False))
                    blink = bool(getattr(cell, "blink", False))
                    style = (fg, bg, bold, underscore, reverse, blink)
                    char = cell.data or " "

                if style != last_style:
                    line_parts.append(style_to_sgr(*style))
                    last_style = style
                line_parts.append(char)
            line_parts.append(ANSI_RESET)
            lines.append("".join(line_parts))
        return lines


# ---------------------------------------------------------------------------
# SGR helpers
# ---------------------------------------------------------------------------


_HEX_CHARS = frozenset("0123456789abcdef")


def _is_hex_color(value: str) -> bool:
    """Return True if *value* is a 6-character hex RGB string (e.g. ``"ff8000"``)."""
    return len(value) == 6 and all(c in _HEX_CHARS for c in value.lower())


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse a 6-char hex string into (R, G, B) integers."""
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _color_sgr(color: str, *, is_fg: bool) -> list[int]:
    """Return SGR codes for a pyte color value.

    Handles three formats:
    - ``"default"`` -> no codes
    - Named 16-color (``"red"``, ``"brightcyan"``) -> single code from FG/BG tables
    - 6-char hex (``"ff8000"``) -> truecolor ``38;2;R;G;B`` or ``48;2;R;G;B``
    """
    if color == "default":
        return []
    table = FG_CODES if is_fg else BG_CODES
    if color in table:
        return [table[color]]
    if _is_hex_color(color):
        r, g, b = _hex_to_rgb(color)
        base = 38 if is_fg else 48
        return [base, 2, r, g, b]
    return []


def _attr_codes(bold: bool, underscore: bool, blink: bool) -> list[int]:
    codes: list[int] = []
    if bold:
        codes.append(1)
    if underscore:
        codes.append(4)
    if blink:
        codes.append(5)
    return codes


def style_to_sgr(
    fg: str,
    bg: str,
    bold: bool,
    underscore: bool,
    reverse: bool,
    blink: bool,
) -> str:
    """Convert pyte cell style attributes to an SGR escape sequence.

    Supports named 16-color, 256-color (pyte hex), and truecolor (pyte hex).
    """
    if reverse:
        fg, bg = bg, fg
    codes = _attr_codes(bold, underscore, blink)
    codes.extend(_color_sgr(fg, is_fg=True))
    codes.extend(_color_sgr(bg, is_fg=False))
    if not codes:
        return ANSI_RESET
    return f"\x1b[{';'.join(str(c) for c in codes)}m"
