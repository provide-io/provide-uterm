#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic line editor for terminal input with readline-style shortcuts.

Provides a stateful line editor that can be used by any terminal session.
Supports:
- Character accumulation until Enter
- Backspace/Delete handling with cursor position tracking
- Readline shortcuts (Ctrl+A, Ctrl+E, Ctrl+U, Ctrl+K, Ctrl+B, Ctrl+F, Ctrl+W)
- Password masking
- Configurable line length limits
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class LineEditor:
    """Generic line editor for terminal sessions.

    Accumulates input characters until Enter is pressed, with support for
    readline-style editing shortcuts and password masking.

    Features:
        - Character-by-character buffering until Enter/Return
        - Full cursor position tracking enabling mid-line editing
        - Backspace/Delete handling (removes character before cursor)
        - Readline shortcuts: Ctrl+A (start of line), Ctrl+E (end of line),
          Ctrl+U (kill backward to start), Ctrl+K (kill forward to end),
          Ctrl+B (left one char), Ctrl+F (right one char),
          Ctrl+W (kill word backward)
        - Password masking: echoes '*' instead of actual characters
        - Configurable maximum line length (prevents DoS)
        - Optional async write callback for terminal output

    Terminal Assumptions:
        - Assumes VT100-compatible terminal (ANSI escape codes)
        - This is true for all BBS systems (Telnet, SSH, WebSocket)
        - Cursor movement uses relative ANSI sequences so the editor does
          not need to know the screen column where input began

    Args:
        max_length: Maximum number of characters to accept (default 80).
        password_mode: If True, mask input with asterisks (default False).
        on_write: Async callback(data: str) for terminal output. Called for
            all output including echoes and cursor movements. Exceptions
            propagate to caller. If None, no output is sent (silent mode).

    Example:
        >>> async def on_write(data: str) -> None:
        ...     await session.send(data)
        >>> editor = LineEditor(max_length=40, password_mode=False,
        ...                      on_write=on_write)
        >>> line = None
        >>> for ch in user_input:
        ...     line = await editor.process_char(ch)
        ...     if line is not None:
        ...         print(f"Got line: {line}")
    """

    def __init__(
        self,
        max_length: int = 80,
        password_mode: bool = False,
        on_write: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.max_length = max_length
        self.password_mode = password_mode
        self.on_write = on_write
        self.buffer = ""
        self.cursor_pos = 0  # index into buffer; 0 = before first char

    async def _emit(self, text: str) -> None:
        """Write to terminal if output callback is set."""
        if self.on_write:
            await self.on_write(text)

    def _display(self, s: str) -> str:
        """Return displayable version of s (masked in password mode)."""
        return "*" * len(s) if self.password_mode else s

    async def process_char(self, ch: str) -> str | None:
        """Process a single character.

        Args:
            ch: Single character input.

        Returns:
            Completed line if Enter was pressed, None otherwise.
        """
        # ── Enter ──────────────────────────────────────────────────────────
        if ch in ("\r", "\n"):
            result = self.buffer
            self.buffer = ""
            self.cursor_pos = 0
            await self._emit("\r\n")
            return result

        # ── Backspace / Delete ─────────────────────────────────────────────
        if ch in ("\x7f", "\x08"):
            if self.cursor_pos > 0:
                tail = self.buffer[self.cursor_pos :]
                self.buffer = self.buffer[: self.cursor_pos - 1] + tail
                self.cursor_pos -= 1
                display = self._display(tail)
                # Move left 1, redraw tail, overwrite extra char at end, move back
                seq = "\x08" + display + " " + f"\x1b[{len(tail) + 1}D"
                await self._emit(seq)
            return None

        # ── Ctrl+A: move to beginning of line ─────────────────────────────
        if ch == "\x01":
            if self.cursor_pos > 0:
                await self._emit(f"\x1b[{self.cursor_pos}D")
                self.cursor_pos = 0
            return None

        # ── Ctrl+E: move to end of line ────────────────────────────────────
        if ch == "\x05":
            n = len(self.buffer) - self.cursor_pos
            if n > 0:
                await self._emit(f"\x1b[{n}C")
                self.cursor_pos = len(self.buffer)
            return None

        # ── Ctrl+B: move left one character ───────────────────────────────
        if ch == "\x02":
            if self.cursor_pos > 0:
                await self._emit("\x1b[D")
                self.cursor_pos -= 1
            return None

        # ── Ctrl+F: move right one character ──────────────────────────────
        if ch == "\x06":
            if self.cursor_pos < len(self.buffer):
                await self._emit("\x1b[C")
                self.cursor_pos += 1
            return None

        # ── Ctrl+U: kill backward (cursor to start of line) ───────────────
        if ch == "\x15":
            if self.cursor_pos > 0:
                remaining = self.buffer[self.cursor_pos :]
                self.buffer = remaining
                seq = f"\x1b[{self.cursor_pos}D"  # move to start of input
                seq += self._display(remaining)  # redraw remaining chars
                seq += "\x1b[K"  # erase from here to EOL
                if remaining:
                    seq += f"\x1b[{len(remaining)}D"  # cursor back to start
                await self._emit(seq)
                self.cursor_pos = 0
            return None

        # ── Ctrl+K: kill forward (cursor to end of line) ──────────────────
        if ch == "\x0b":
            if self.cursor_pos < len(self.buffer):
                self.buffer = self.buffer[: self.cursor_pos]
                await self._emit("\x1b[K")
            return None

        # ── Ctrl+W: kill word backward ─────────────────────────────────────
        if ch == "\x17":
            if self.cursor_pos > 0:
                pos = self.cursor_pos
                while pos > 0 and self.buffer[pos - 1] == " ":
                    pos -= 1
                while pos > 0 and self.buffer[pos - 1] != " ":
                    pos -= 1
                deleted = self.cursor_pos - pos
                remaining = self.buffer[self.cursor_pos :]
                self.buffer = self.buffer[:pos] + remaining
                seq = f"\x1b[{deleted}D"
                seq += self._display(remaining)
                seq += "\x1b[K"
                if remaining:
                    seq += f"\x1b[{len(remaining)}D"
                await self._emit(seq)
                self.cursor_pos = pos
            return None

        # ── Regular character insertion ────────────────────────────────────
        if len(self.buffer) < self.max_length:
            tail = self.buffer[self.cursor_pos :]
            self.buffer = self.buffer[: self.cursor_pos] + ch + tail
            self.cursor_pos += 1
            if not tail:
                # Inserting at end: simple echo
                await self._emit("*" if self.password_mode else ch)
            else:
                # Mid-line insert: echo new char + redraw tail, move cursor back
                display = self._display(ch + tail)
                seq = display + f"\x1b[{len(tail)}D"
                await self._emit(seq)
        return None

    def reset(self) -> None:
        """Reset the buffer and cursor to empty state."""
        self.buffer = ""
        self.cursor_pos = 0

    def get_buffer(self) -> str:
        """Get current buffer contents."""
        return self.buffer

    def set_max_length(self, length: int) -> None:
        """Change the maximum line length."""
        self.max_length = length

    def set_password_mode(self, enabled: bool) -> None:
        """Enable or disable password masking."""
        self.password_mode = enabled
