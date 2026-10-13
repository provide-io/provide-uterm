#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate ``relative_paint_80x24.bin``, the snapshot-resume fixture.

A viewer that joins a live session is painted from a snapshot and then fed the
rest of the stream. The stream does not start over for it: every byte after the
snapshot assumes the terminal is already in whatever state the bytes before it
left behind. This file is a stream that leans on exactly that state, so a
snapshot that drops any of it shows up as a different screen.

It is modelled on a capture of vitetris 0.59, which never addresses the cursor
absolutely: it paints with CUU/CUD/CUF/CUB, CR and LF only, keeps the pen across
moves, hides the cursor, and lets the bottom of the screen scroll. On top of that
it walks through the other state a snapshot has to carry: a DECSTBM scroll
region with origin mode, autowrap off, insert mode, the pending wrap at the last
column, DEC special graphics through G0 and G1 with SO/SI, line-feed/new-line
mode, application cursor keys, bracketed paste and 256/true-colour pens.

Every port replays the same file (see each port's snapshot-resume test), so it is
generated once and committed. The generator is deterministic and has no inputs;
it is kept for provenance, not run in CI. Regenerate with::

    uv run python conformance/snapshot/gen_relative_paint.py

The output is pure 7-bit ASCII, so a CP437 and a UTF-8 session decode it alike.
"""

from __future__ import annotations

import random
from pathlib import Path

ESC = "\x1b"
SO = "\x0e"
SI = "\x0f"
OUT = Path(__file__).with_name("relative_paint_80x24.bin")

#: The pens vitetris paints its pieces with: a background, then a bold
#: foreground of the same hue. 43/33 is the one pyte calls "brown".
PIECE_PENS = ("41;1;31", "44;1;34", "46;1;36", "47;1;37", "42;1;32", "43;1;33", "45;1;35")


def csi(body: str) -> str:
    return f"{ESC}[{body}"


def title(parts: list[str]) -> None:
    """The splash: seven pieces drawn with relative moves only."""
    parts.append(csi("?25l"))
    parts.append("\r\n" * 4)
    parts.append(csi("23C"))
    for index, pen in enumerate(PIECE_PENS):
        parts.append(csi(pen + "m"))
        parts.append(csi(f"{1 + index % 3}C"))
        parts.append(csi("2A") if index % 2 else csi("1A"))
        parts.append("# # ")
        parts.append(csi("4D") + csi("1B") + "# ")
        parts.append(csi("2D") + csi("1B") + "# #")
    parts.append(csi("m"))
    parts.append(csi("12D") + csi("2B") + csi("1m") + "relative paint 1.0" + csi("m"))
    parts.append("\r\n" + csi("24C") + csi("44m") + csi("1;37m") + " 1-Player Game " + csi("m"))
    parts.append("\r\n" + csi("24C") + csi("1;35m") + " Level " + csi("m") + csi("1C"))
    parts.append(csi("7m") + "0" + csi("m") + " 1 2 3 4 5 6 7 8 9")
    parts.append("\r\n" + csi("24C") + csi("K") + csi("4m") + "underlined" + csi("24m") + csi("5m") + " blink")
    parts.append(csi("m"))


def board(parts: list[str]) -> None:
    """A walled well, its sides drawn in DEC special graphics through G1."""
    parts.append("\r\n" * 2)
    parts.append(ESC + ")0")
    for _ in range(14):
        parts.append(csi("2C") + SO + "x" + SI + csi("20C") + SO + "x" + SI + "\r\n")
    parts.append(csi("2C") + SO + "m" + "q" * 20 + "j" + SI)
    # G0 switched to graphics as well, and left there for a while.
    parts.append(csi("15A") + ESC + "(0" + "lqk" + ESC + "(B" + csi("3D"))


def fall(parts: list[str], rng: random.Random, rounds: int) -> None:
    """Pieces falling: erase, step down, redraw -- never an absolute address."""
    for _ in range(rounds):
        pen = rng.choice(PIECE_PENS)
        column = rng.randint(1, 8)
        parts.append(csi(f"{column}C"))
        for step in range(rng.randint(3, 7)):
            parts.append(csi(pen + "m") + "[]" + csi("2D"))
            parts.append(csi("m") + "  " + csi("2D") + csi("1B") if step % 2 else csi("1B"))
            parts.append(csi(pen + "m") + "[]" + csi("2D"))
        # Leave the pen set: the next move must not be painted in the default
        # background on a viewer that missed the SGR.
        parts.append(csi(pen + "m") + csi("3B") + csi(f"{column}D") + "\r")
        parts.append(csi("4A") + csi("30C") + csi("m") + f"Score {rng.randint(0, 99999):5d}" + csi("K"))
        parts.append("\r" + csi("2B"))


def edges(parts: list[str]) -> None:
    """The last column: the pending wrap, autowrap off and insert mode."""
    # Fill to the last column and stop there: the cursor now sits in the
    # deferred-wrap state. Change the pen while it does, then draw.
    parts.append("\r" + csi("76C") + csi("1;32m") + "WRAP" + csi("0;35m") + "+next line")
    parts.append(csi("m") + "\r\n")
    parts.append(csi("?7l") + csi("70C") + "no autowrap here, so this overprints the last cell" + csi("?7h"))
    parts.append("\r\n" + csi("4h") + "inserted " + csi("4l") + "\r" + csi("4h") + ">>")
    parts.append(csi("4l"))


def region(parts: list[str], rng: random.Random) -> None:
    """A scroll region with origin mode, scrolled from inside."""
    parts.append(csi("5;18r") + csi("?6h"))
    parts.append(csi("3B") + csi("5C") + csi("38;5;208m") + "in the region" + csi("m"))
    for line in range(16):
        parts.append("\r\n" + csi(f"{rng.randint(0, 40)}C") + csi("48;2;10;20;30m") + f"scroll {line}")
    parts.append(csi("3;9m") + " italic struck" + csi("23;29m"))
    parts.append(csi("?6l") + csi("21B") + "\r" + csi("m") + "below the region")
    parts.append(csi("r"))


def modes(parts: list[str]) -> None:
    """Modes that change what later bytes mean."""
    parts.append(csi("20h") + csi("10C") + "LNM" + "\n" + "fed" + csi("20l"))
    parts.append(csi("?1h") + csi("?2004h") + csi("?1000h") + csi("?1006h"))
    parts.append("\r\n" + ESC + "(0" + "lqqk" + "\r\n" + "x  x")


def scroll_off(parts: list[str], rng: random.Random) -> None:
    """Run off the bottom so the top scrolls away, as vitetris does."""
    parts.append(ESC + "(B" + csi("23B"))
    for _ in range(6):
        parts.append("\r\n" + csi(f"{rng.randint(0, 50)}C") + csi(rng.choice(PIECE_PENS) + "m") + "scroll")
    parts.append(csi("m") + csi("?25h") + csi("?1l") + csi("?2004l") + csi("?1000l") + csi("?1006l"))


def build() -> str:
    rng = random.Random(20261013)
    parts: list[str] = []
    title(parts)
    board(parts)
    fall(parts, rng, rounds=6)
    edges(parts)
    region(parts, rng)
    fall(parts, rng, rounds=3)
    modes(parts)
    scroll_off(parts, rng)
    fall(parts, rng, rounds=3)
    return "".join(parts)


def main() -> None:
    text = build()
    assert text.isascii()
    OUT.write_bytes(text.encode("ascii"))
    print(f"wrote {OUT} ({len(text)} bytes)")


if __name__ == "__main__":
    main()
