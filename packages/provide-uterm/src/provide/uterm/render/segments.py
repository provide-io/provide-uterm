#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Render colored text into structured segments for non-terminal clients.

A terminal client (xterm, telnet) interprets ANSI escapes itself. A structured
client — e.g. a web "deck" UI that renders React spans rather than a character
grid — needs the same color information as *data*, not as escape bytes.

``tokens_to_segments`` produces that data WITHOUT a second copy of the color
dialect: it runs the canonical ``normalize_colors`` (brace/tilde/pipe tokens ->
ANSI) and then parses that very ANSI back into ``[Segment(text, color, bold)]``.
Because the segment stream is derived from the same ANSI the terminal renders,
the two presentations cannot drift.

Colors are emitted as semantic names (``"red"``, ``"green"`` …) so the client
can map them onto its own theme rather than baking in RGB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from provide.uterm.ansi import normalize_colors

__all__ = ["SEGMENT_COLOR_NAMES", "Segment", "ansi_to_segments", "tokens_to_segments"]

# Standard SGR foreground codes -> semantic color names. 30-37 are the base
# eight; 90-97 are the "bright" aliases (rendered bold here so a client without
# a separate bright palette still distinguishes them).
_SGR_FG: dict[int, str] = {
    30: "black",
    31: "red",
    32: "green",
    33: "yellow",
    34: "blue",
    35: "magenta",
    36: "cyan",
    37: "white",
}

#: The closed set of color names a segment may carry (``None`` = default).
SEGMENT_COLOR_NAMES: tuple[str, ...] = tuple(_SGR_FG.values())

# One SGR sequence: ESC [ <params> m
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
# Any other CSI / escape (cursor moves, clears) - dropped, they carry no text.
_OTHER_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b.")


@dataclass(frozen=True, slots=True)
class Segment:
    """A run of text sharing one foreground color + bold flag."""

    text: str
    color: str | None = None
    bold: bool = False


def _apply_sgr(params: str, color: str | None, bold: bool) -> tuple[str | None, bool]:
    """Fold one SGR parameter list into the running (color, bold) state."""
    codes = [int(p) if p else 0 for p in params.split(";")] if params else [0]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            color, bold = None, False
        elif c == 1:
            bold = True
        elif c == 22:
            bold = False
        elif c == 39:
            color = None
        elif 30 <= c <= 37:
            color = _SGR_FG[c]
        elif 90 <= c <= 97:
            color, bold = _SGR_FG[c - 60], True
        elif c in (38, 48):
            # Extended color: skip its operands (5;n or 2;r;g;b) so they are not
            # mis-read as further SGR codes. Falls back to the default color.
            if i + 1 < len(codes) and codes[i + 1] == 5:
                i += 2
            elif i + 1 < len(codes) and codes[i + 1] == 2:
                i += 4
        i += 1
    return color, bold


def ansi_to_segments(text: str) -> list[Segment]:
    """Parse ANSI-colored ``text`` into a list of :class:`Segment`.

    Recognizes SGR color/bold/reset; other escapes (cursor, clear) are dropped.
    Adjacent runs with identical style are merged; empty runs are skipped.
    """
    segments: list[Segment] = []
    color: str | None = None
    bold = False
    buf: list[str] = []

    def flush() -> None:
        if buf:
            chunk = "".join(buf)
            if segments and segments[-1].color == color and segments[-1].bold == bold:
                segments[-1] = Segment(segments[-1].text + chunk, color, bold)
            else:
                segments.append(Segment(chunk, color, bold))
            buf.clear()

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\x1b":
            buf.append(ch)
            i += 1
            continue
        sgr = _SGR_RE.match(text, i)
        if sgr is not None:
            flush()
            color, bold = _apply_sgr(sgr.group(1), color, bold)
            i = sgr.end()
            continue
        other = _OTHER_ESC_RE.match(text, i)
        if other is not None:
            i = other.end()  # drop the non-SGR escape, emit no text
            continue
        # Lone ESC with nothing after - skip it.
        i += 1
    flush()
    return segments


def tokens_to_segments(text: str) -> list[Segment]:
    """Render dialect-token ``text`` (``{+g}…{-x}`` etc.) into color segments.

    Equivalent to ``ansi_to_segments(normalize_colors(text))`` — the segment
    colors are derived from the same ANSI the terminal renders, so they cannot
    drift from the token dialect.
    """
    return ansi_to_segments(normalize_colors(text))
