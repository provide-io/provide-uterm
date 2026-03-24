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

    async def _emit(self, text: str) -> None:
        """Write to terminal if output callback is set."""
        if self.on_write:
            await self.on_write(text)

    async def _apply_cursor_shortcut(self, ch: str) -> bool:
        """Handle Ctrl+A/E cursor movement shortcuts. Returns True if handled."""
        if ch == "\x01":  # Ctrl+A: move to beginning of line
            if self.buffer:
                await self._emit("\x1b[H")
            return True
        if ch == "\x05":  # Ctrl+E: move to end of line
            if self.buffer:
                await self._emit(f"\x1b[{len(self.buffer)}G")
            return True
        return False

    async def _apply_clear_shortcut(self, ch: str) -> bool:
        """Handle Ctrl+U/K line-clear shortcuts. Returns True if handled."""
        if ch == "\x15":  # Ctrl+U: delete entire line
            if self.buffer:
                self.buffer = ""
                if self.on_write:
                    await self.on_write("\x1b[2K\r")
            return True
        if ch == "\x0b":  # Ctrl+K: delete to end of line
            if self.buffer:
                self.buffer = ""
                if self.on_write:
                    await self.on_write("\x1b[K")
            return True
        return False

    async def _apply_edit_shortcut(self, ch: str) -> bool:
        """Handle Ctrl+A/E/U/K readline shortcuts. Returns True if the character was handled."""
        return await self._apply_cursor_shortcut(ch) or await self._apply_clear_shortcut(ch)

    async def process_char(self, ch: str) -> str | None:
        """Process a single character.

        Args:
            ch: Single character input.

        Returns:
            Completed line if Enter was pressed, None otherwise.
        """
        if ch in ("\r", "\n"):
            result = self.buffer
            self.buffer = ""
            await self._emit("\r\n")
            return result
        if ch in ("\x7f", "\x08"):
            if self.buffer:
                self.buffer = self.buffer[:-1]
                await self._emit("\x08 \x08")
            return None
        if await self._apply_edit_shortcut(ch):
            return None
        if len(self.buffer) < self.max_length:
            self.buffer += ch
            await self._emit("*" if self.password_mode else ch)
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
