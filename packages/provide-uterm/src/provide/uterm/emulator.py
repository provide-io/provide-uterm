#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Terminal emulation via pyte.

Requires the ``emulator`` extra::

    pip install 'provide-uterm[emulator]'
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

try:
    import pyte
except ImportError as _e:  # pragma: no cover
    raise ImportError("pyte is required for TerminalEmulator: pip install 'provide-uterm[emulator]'") from _e

_CP437 = "cp437"


def _parse_screen_text(screen: pyte.Screen) -> str:
    return "\n".join(screen.display)


class TerminalEmulator:
    """VT/ANSI terminal emulator backed by pyte.

    Args:
        cols: Terminal width in columns (default 80).
        rows: Terminal height in rows (default 25).
        term: Terminal type string (default ``"ANSI"``).

    Memory / scrollback bounds:
        Backed by ``pyte.Screen``, which is **bounded**: only the visible
        viewport (``cols * rows`` cells) is retained. Scrolling overwrites
        rather than buffering. There is no off-screen scrollback in this
        layer — applications that need history must record the raw byte
        stream separately (see :mod:`provide.uterm.replay`). This means
        ``get_snapshot`` and ``ansi_screen`` always allocate O(cols*rows),
        independent of session age. ``resize`` is also O(cols*rows): pyte
        re-flows the buffer, clipping rows that no longer fit.
    """

    def __init__(self, cols: int = 80, rows: int = 25, term: str = "ANSI") -> None:
        self.cols = cols
        self.rows = rows
        self.term = term
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._dirty = True
        self._last_snapshot: dict[str, Any] | None = None

    def process(self, data: bytes) -> None:
        """Feed raw bytes (CP437) through the emulator.

        Args:
            data: Raw bytes from a transport or file.
        """
        self._stream.feed(data.decode(_CP437, errors="replace"))
        self._dirty = True

    def _is_cursor_at_end(self) -> bool:
        # ``len(line) - 2`` slack is a deliberate heuristic, not an off-by-one:
        # BBS-style prompts often leave 1-2 trailing spaces after the input
        # caret (e.g. ``> `` or ``> _``), and detection rules need to treat
        # those as "still at the prompt" rather than "user has typed". A
        # tighter check (``>= len(line)``) misclassified TradeWars 2002 and
        # Major BBS prompts as not-at-end during the 2026-04 detector
        # rewrite; widen-back to 2 chars restored the prior pass rate.
        cursor_x = self._screen.cursor.x
        cursor_y = self._screen.cursor.y
        lines = self._screen.display
        for row_idx in range(len(lines) - 1, -1, -1):
            line = lines[row_idx].rstrip()
            if line:
                if cursor_y == row_idx:
                    return bool(int(cursor_x) >= len(line) - 2)
                return bool(int(cursor_y) > row_idx)
        return True

    def get_snapshot(self) -> dict[str, Any]:
        """Return the current screen state.

        Returns a dict with:

        - ``screen``: Full screen text (newline-separated rows).
        - ``screen_hash``: SHA-256 of the screen text.
        - ``cursor``: ``{"x": int, "y": int}``.
        - ``cols``, ``rows``, ``term``.
        - ``cursor_at_end``: ``True`` if cursor is at or past the last content line.
        - ``has_trailing_space``: ``True`` if the screen ends with a space or colon.
        - ``captured_at``: Unix timestamp of this snapshot (always fresh).
        """
        if self._last_snapshot is None or self._dirty:
            screen_text = _parse_screen_text(self._screen)
            screen_hash = hashlib.sha256(screen_text.encode("utf-8")).hexdigest()
            self._last_snapshot = {
                "screen": screen_text,
                "screen_hash": screen_hash,
                "cursor": {"x": self._screen.cursor.x, "y": self._screen.cursor.y},
                "cols": self.cols,
                "rows": self.rows,
                "term": self.term,
                "cursor_at_end": self._is_cursor_at_end(),
                "has_trailing_space": screen_text.rstrip() != screen_text.rstrip(" :"),
            }
            self._dirty = False

        snap = dict(self._last_snapshot)
        snap["cursor"] = dict(snap.get("cursor") or {"x": 0, "y": 0})
        snap["captured_at"] = time.time()
        return snap

    def ansi_screen(self) -> str:
        """Return the current screen as a single string with ANSI SGR codes.

        Walks pyte's per-cell style buffer and emits SGR escape sequences
        whenever the style changes between adjacent cells. Use this when
        a downstream consumer (xterm.js dashboard, AnsiBuffer in a spy)
        needs the *visual* state including colors — :meth:`get_snapshot`'s
        plain ``screen`` field discards pyte's style attributes.

        Rows are joined with ``\\n``; each row ends with ``\\x1b[0m`` so a
        consumer's subsequent writes start from a clean attribute state.
        """
        # Local import keeps the render module out of the import-time
        # graph for callers that only need plain-text snapshots.
        from provide.uterm.render.buffer import ANSI_RESET, style_to_sgr

        rows_out: list[str] = []
        buffer = self._screen.buffer
        for y in range(self.rows):
            row: dict[int, Any] = buffer.get(y, {})
            parts: list[str] = []
            last_style: tuple[str, str, bool, bool, bool, bool] | None = None
            for x in range(self.cols):
                cell = row.get(x)
                if cell is None:
                    char = " "
                    style = ("default", "default", False, False, False, False)
                else:
                    style = (
                        cell.fg or "default",
                        cell.bg or "default",
                        bool(cell.bold),
                        bool(getattr(cell, "underscore", False)),
                        bool(getattr(cell, "reverse", False)),
                        bool(getattr(cell, "blink", False)),
                    )
                    char = cell.data or " "
                if style != last_style:
                    parts.append(style_to_sgr(*style))
                    last_style = style
                parts.append(char)
            parts.append(ANSI_RESET)
            rows_out.append("".join(parts))
        return "\n".join(rows_out)

    def reset(self) -> None:
        """Reset terminal to its initial state."""
        self._screen.reset()
        self._dirty = True

    def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal.

        Args:
            cols: New width in columns.
            rows: New height in rows.
        """
        self.cols = cols
        self.rows = rows
        # pyte's Screen.resize signature is (lines, columns) == (rows, cols),
        # unlike the constructor pyte.Screen(columns, lines). See
        # render/buffer.py:103 for the correct reference.
        self._screen.resize(rows, cols)
        self._dirty = True
