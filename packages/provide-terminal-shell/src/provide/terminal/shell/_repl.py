#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Terminal line-buffer for ushell.

Handles raw keystroke bytes from xterm.js and accumulates them into
complete lines.  Callers feed raw data with :meth:`LineBuffer.feed` and
then drain echo and completed lines separately.

Protocol notes
--------------
- xterm.js sends keystrokes as individual ``term`` frames; each frame may
  contain one or more bytes.
- Enter → ``\\r`` (carriage return).  Some clients send ``\\r\\n``.
- Backspace → ``\\x7f`` (DEL) on most systems; ``\\x08`` (BS) on some.
- Ctrl+C → ``\\x03``.
- Ctrl+D → ``\\x04`` (treated as empty-submit in v1).
- Arrow keys / F-keys / other VT sequences start with ``\\x1b[`` and are
  swallowed (no history navigation in v1).
"""

from __future__ import annotations

_CTRL_C = "\x03"
_CTRL_D = "\x04"


class LineBuffer:
    """Stateful line editor that accumulates raw keystroke bytes.

    Usage::

        buf = LineBuffer()
        buf.feed(raw_data)
        echo = buf.take_echo()          # bytes to echo back to terminal
        lines = buf.take_completed()    # fully-entered lines (may be empty)
    """

    def __init__(self, *, max_line: int = 4096) -> None:
        self._max_line = max_line
        self._buf: list[str] = []
        self._echo: list[str] = []
        self._completed: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, data: str) -> None:
        """Process *data* (raw keystroke bytes) and update internal state."""
        i = 0
        while i < len(data):
            ch = data[i]

            if ch in ("\r", "\n"):
                # Skip LF immediately following CR (handles \r\n sequences).
                if ch == "\r" and i + 1 < len(data) and data[i + 1] == "\n":
                    i += 1
                self._echo.append("\r\n")
                self._completed.append("".join(self._buf))
                self._buf.clear()
                i += 1

            elif ch in ("\x7f", "\x08"):  # DEL or BS — backspace
                if self._buf:
                    self._buf.pop()
                    self._echo.append("\x08 \x08")
                i += 1

            elif ch == _CTRL_C:
                self._buf.clear()
                self._echo.append("^C\r\n")
                self._completed.append(_CTRL_C)
                i += 1

            elif ch == _CTRL_D:
                # Treat like Enter with empty line — lets caller handle EOF.
                self._echo.append("\r\n")
                self._completed.append("".join(self._buf) if self._buf else _CTRL_D)
                self._buf.clear()
                i += 1

            elif ch == "\x1b":
                # VT escape sequence — swallow entirely (arrow keys, F-keys, etc.).
                i = LineBuffer._consume_escape(data, i)

            elif ch >= " " or ch == "\t":
                # Printable character (or tab — shown as-is).
                if len(self._buf) < self._max_line:
                    self._buf.append(ch)
                    self._echo.append(ch)
                i += 1

            else:
                # Other control bytes — ignore silently.
                i += 1

    def take_echo(self) -> str:
        """Return accumulated echo string and clear the internal buffer."""
        s = "".join(self._echo)
        self._echo.clear()
        return s

    def take_completed(self) -> list[str]:
        """Return completed lines and clear the internal list."""
        lines = self._completed[:]
        self._completed.clear()
        return lines

    def current_line(self) -> str:
        """Return the current (uncommitted) line buffer contents."""
        return "".join(self._buf)

    def clear(self) -> None:
        """Discard current line buffer without emitting a completed line."""
        self._buf.clear()
        self._echo.clear()

    @staticmethod
    def _consume_escape(data: str, i: int) -> int:
        """Consume a VT escape sequence starting at data[i]. Returns index of next character."""
        j = i + 1
        if j < len(data) and data[j] == "[":
            j += 1
            # Consume parameter bytes (0x30-0x3F) and intermediate bytes (0x20-0x2F)
            while j < len(data) and data[j] < "\x40":
                j += 1
            # Consume the final byte (0x40-0x7E)
            if j < len(data) and "\x40" <= data[j] <= "\x7e":
                j += 1
        elif j < len(data) and data[j] == "O":
            # SS3 sequences (e.g. F1-F4 on some terminals)
            j += 1
            if j < len(data):
                j += 1
        return j
