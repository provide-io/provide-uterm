#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The terminal state a screen snapshot has to carry besides its cells.

A viewer that joins a live session is painted from a snapshot and then fed the
rest of the stream, and that stream does not start over for it: each byte
assumes the cursor, pen and modes the bytes before it left. A program that
paints with relative movement (vitetris never sends CUP) therefore drew its
whole board in the wrong place on a viewer whose snapshot left the cursor at
the bottom-right of the painted rows, and in the default background on one that
never saw its last SGR.

:func:`screen_state_prefix` and :func:`screen_state_suffix` bracket the painted
rows so that a fresh terminal ends up in the emulator's state: entering the
alternate screen before the paint (entering it clears), and after it the scroll
region, origin mode, cursor position -- including the deferred wrap at the last
column -- autowrap, insert and new-line modes, the input modes a controlling
viewer must honour, the G0/G1 designations and shift, the pen, and cursor
visibility.

The Go, C# and TypeScript emulators emit the same bytes; the snapshot-resume
fixture in ``conformance/snapshot`` holds all four to it.
"""

from __future__ import annotations

from typing import Any

import pyte
from pyte import charsets as cs
from pyte import modes as mo

from provide.uterm.render.buffer import ANSI_HIDE_CURSOR, ANSI_SHOW_CURSOR, _color_sgr, cell_style, style_to_sgr

#: DECCKM, application cursor keys, stored the way pyte stores private modes.
DECCKM = 1 << 5

#: Alternate-screen modes, in the order they are re-entered.
_ALT_SCREEN_MODES = (47, 1047, 1049)

#: Private modes that change what a controlling viewer's keyboard and mouse
#: send. Restored only when set: a viewer that never had them needs no reset.
_INPUT_MODES = (1000, 1002, 1003, 1005, 1006, 1015, 2004)

#: The designator for each charset pyte can hold, by identity of its table.
_CHARSET_CODES = {id(table): code for code, table in cs.MAPS.items()}

#: SGR parameter for each pen flag, in emission order.
_PEN_FLAGS = (("bold", 1), ("italics", 3), ("underscore", 4), ("blink", 5), ("reverse", 7), ("strikethrough", 9))


def pen_sgr(attrs: Any) -> str:
    """The SGR sequence that sets a fresh terminal's pen to *attrs*.

    Starts from a reset so it does not depend on the pen it lands on, and keeps
    reverse as reverse: swapping the colours instead is no change at all when
    both are the default.
    """
    codes = [0]
    codes.extend(code for name, code in _PEN_FLAGS if getattr(attrs, name))
    codes.extend(_color_sgr(attrs.fg or "default", is_fg=True))
    codes.extend(_color_sgr(attrs.bg or "default", is_fg=False))
    return f"\x1b[{';'.join(str(code) for code in codes)}m"


def screen_state_prefix(screen: pyte.Screen) -> str:
    """What must precede the painted rows: the alternate screen, if active.

    It cannot follow them, because entering the alternate screen clears it.
    """
    entered = "".join(f"\x1b[?{mode}h" for mode in _ALT_SCREEN_MODES if mode << 5 in screen.mode)
    return f"{entered}\x1b[H" if entered else ""


def _cursor_placement(screen: pyte.Screen, row: int) -> str:
    """Move to the cursor, reproducing the deferred wrap at the last column.

    pyte records a cursor that has just drawn into the last column as
    ``x == columns``: the next printable wraps first. No CUP can address that
    position, so the last cell is drawn again, in its own style, from the column
    before -- which leaves any terminal in the same deferred state. A wide
    character there occupies the last two cells and is redrawn whole.
    """
    columns = screen.columns
    if screen.cursor.x < columns:
        return f"\x1b[{row};{screen.cursor.x + 1}H"
    line = screen.buffer[screen.cursor.y]
    x = columns - 1
    if line[x].data == "" and x > 0:
        x -= 1
    cell = line[x]
    return f"\x1b[{row};{x + 1}H{style_to_sgr(*cell_style(cell))}{cell.data or ' '}"


def _charset_state(screen: pyte.Screen) -> str:
    """The G0/G1 designations and the shift, unless nothing has touched them.

    pyte powers on with G0 Latin-1 (``B``), G1 DEC graphics (``0``) and G0
    shifted in. A screen still in that state that never saw a designation sends
    nothing: a snapshot is also read as text, and ``strip_ansi`` does not remove
    designations, so sending them every time would leave ``(B)0`` in every
    stripped UTF-8 screen. Once the stream has designated a set, they are sent
    even when they match the power-on state -- a viewer's G1 powers on as
    ASCII, so a program that designated G1 as graphics before it joined would
    otherwise draw letters at its next SO.
    """
    g0 = _CHARSET_CODES[id(screen.g0_charset)]
    g1 = _CHARSET_CODES[id(screen.g1_charset)]
    designated = bool(getattr(screen, "charsets_designated", False))
    if (g0, g1, screen.charset) == ("B", "0", 0) and not designated:
        return ""
    shift = "\x0e" if screen.charset else ""
    return f"\x1b({g0}\x1b){g1}{shift}"


def screen_state_suffix(screen: pyte.Screen, *, charsets: bool) -> str:
    """What must follow the painted rows to leave a terminal in *screen*'s state.

    *charsets* says whether the stream honours designations and SO/SI at all.
    pyte ignores them in a CP437 session, where there is no state to restore and
    a stray SO in ANSI art must stay a literal.

    The order is load-bearing. DECSTBM and DECOM both home the cursor, so they
    come before the cursor is placed; the deferred-wrap redraw needs autowrap on
    and insert mode off, so the real values of those come after it.
    """
    mode = screen.mode
    # A region covering the whole screen is no region: sent as the reset, the
    # same bytes from every port whether its emulator keeps "none" or "all".
    margins = screen.margins if screen.margins != (0, screen.lines - 1) else None
    origin = mo.DECOM in mode
    parts = [f"\x1b[{margins.top + 1};{margins.bottom + 1}r" if margins else "\x1b[r"]
    parts.append("\x1b[?6h" if origin else "\x1b[?6l")
    parts.append("\x1b[?7h\x1b[4l")
    row = screen.cursor.y + 1 - (margins.top if origin and margins else 0)
    parts.append(_cursor_placement(screen, row))
    if mo.DECAWM not in mode:
        parts.append("\x1b[?7l")
    if mo.IRM in mode:
        parts.append("\x1b[4h")
    parts.append("\x1b[20h" if mo.LNM in mode else "\x1b[20l")
    parts.append("\x1b[?1h" if DECCKM in mode else "\x1b[?1l")
    parts.extend(f"\x1b[?{input_mode}h" for input_mode in _INPUT_MODES if input_mode << 5 in mode)
    if charsets:
        parts.append(_charset_state(screen))
    parts.append(pen_sgr(screen.cursor.attrs))
    parts.append(ANSI_HIDE_CURSOR if screen.cursor.hidden else ANSI_SHOW_CURSOR)
    return "".join(parts)
