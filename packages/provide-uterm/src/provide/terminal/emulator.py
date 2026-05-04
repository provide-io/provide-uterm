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
        self._screen.resize(cols, rows)
        self._dirty = True
