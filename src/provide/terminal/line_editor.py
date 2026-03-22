#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic line editor for terminal input with readline-style shortcuts.

Provides a stateful line editor that can be used by any terminal session.
Supports:
- Character accumulation until Enter
- Backspace/Delete handling
- Readline shortcuts (Ctrl+A, Ctrl+E, Ctrl+U, Ctrl+K)
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
        - Backspace/Delete handling (removes last character)
        - Readline shortcuts: Ctrl+A (start), Ctrl+E (end), Ctrl+U (clear),
          Ctrl+K (clear EOL)
        - Password masking: echoes '*' instead of actual characters
        - Configurable maximum line length (prevents DoS)
        - Optional async write callback for terminal output

    Terminal Assumptions:
        - Assumes VT100-compatible terminal (ANSI escape codes)
        - This is true for all BBS systems (Telnet, SSH, WebSocket)
        - Cursor positioning (Ctrl+A/E) uses: Home key (\\x1b[H) and
          absolute column positioning (\\x1b[Col]G)

    Readline Behavior Notes:
        - Ctrl+U and Ctrl+K clear the ENTIRE buffer (not just to cursor)
        - This differs from GNU readline (U=kill-backward, K=kill-forward)
        - Rationale: Simplifies implementation, matches BBS usage patterns
        - Full partial-deletion readline may be added in future versions

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

    async def process_char(self, ch: str) -> str | None:
        """Process a single character.

        Args:
            ch: Single character input.

        Returns:
            Completed line if Enter was pressed, None otherwise.
        """
        # Enter/Return: line is complete
        if ch in ("\r", "\n"):
            result = self.buffer
            self.buffer = ""
            if self.on_write:
                await self.on_write("\r\n")
            return result

        # Backspace/Delete: remove last character
        if ch in ("\x7f", "\x08"):
            if self.buffer:
                self.buffer = self.buffer[:-1]
                if self.on_write:
                    await self.on_write("\x08 \x08")
            return None

        # Ctrl+A: move to beginning of line
        if ch == "\x01":
            if self.buffer and self.on_write:
                await self.on_write("\x1b[H")
            return None

        # Ctrl+E: move to end of line
        if ch == "\x05":
            if self.buffer and self.on_write:
                await self.on_write(f"\x1b[{len(self.buffer)}G")
            return None

        # Ctrl+U: delete from start to cursor (delete entire line)
        if ch == "\x15":
            if self.buffer and self.on_write:
                self.buffer = ""
                await self.on_write("\x1b[2K\r")
            return None

        # Ctrl+K: delete from cursor to end of line
        if ch == "\x0b":
            if self.buffer and self.on_write:
                self.buffer = ""
                await self.on_write("\x1b[K")
            return None

        # Regular character: add to buffer if under limit
        if len(self.buffer) < self.max_length:
            self.buffer += ch
            if self.on_write:
                if self.password_mode:
                    await self.on_write("*")
                else:
                    await self.on_write(ch)
        return None

    def reset(self) -> None:
        """Reset the buffer to empty state."""
        self.buffer = ""

    def get_buffer(self) -> str:
        """Get current buffer contents."""
        return self.buffer

    def set_max_length(self, length: int) -> None:
        """Change the maximum line length."""
        self.max_length = length

    def set_password_mode(self, enabled: bool) -> None:
        """Enable or disable password masking."""
        self.password_mode = enabled
