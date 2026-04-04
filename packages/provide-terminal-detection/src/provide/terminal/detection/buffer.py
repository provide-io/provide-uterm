#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Screen buffering with timing metadata for prompt detection."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from pydantic import BaseModel


class ScreenBuffer(BaseModel):
    """Represents a buffered screen snapshot with timing metadata."""

    screen: str
    screen_hash: str
    snapshot: dict[str, Any]
    captured_at: float
    matched_prompt_id: str | None = None
    time_since_last_change: float = 0.0


class BufferManager:
    """Manages screen history buffer with timing calculation."""

    def __init__(self, max_size: int = 50) -> None:
        """Initialize buffer manager.

        Args:
            max_size: Maximum number of screens to buffer
        """
        self._buffer: deque[ScreenBuffer] = deque(maxlen=max_size)
        self._last_hash: str = ""
        self._last_change_time: float = 0.0

    @property
    def _buffers(self) -> dict[str, deque[ScreenBuffer]]:
        """Backward-compatible view for older tests/code that expected per-session buffers."""
        return {"default": self._buffer}

    def add_screen(self, snapshot: dict[str, Any]) -> ScreenBuffer:
        """Add screen snapshot to buffer and calculate timing metadata.

        Args:
            snapshot: Screen snapshot from terminal emulator

        Returns:
            ScreenBuffer with timing metadata
        """
        now = snapshot.get("captured_at", time.time())
        screen_hash = snapshot["screen_hash"]

        # Detect screen change
        if screen_hash != self._last_hash:
            time_since_change = now - self._last_change_time if self._last_change_time > 0 else 0.0
            self._last_hash = screen_hash
            self._last_change_time = now
        else:
            # Screen unchanged - calculate time since last change
            time_since_change = now - self._last_change_time if self._last_change_time > 0 else 0.0

        buffer = ScreenBuffer(
            screen=snapshot["screen"],
            screen_hash=screen_hash,
            snapshot=snapshot,
            captured_at=now,
            time_since_last_change=time_since_change,
        )
        self._buffer.append(buffer)
        return buffer

    def get_recent(self, n: int = 5) -> list[ScreenBuffer]:
        """Get N most recent buffered screens.

        Args:
            n: Number of recent screens to retrieve

        Returns:
            List of most recent ScreenBuffer objects (oldest first)
        """
        if n >= len(self._buffer):
            return list(self._buffer)
        return list(self._buffer)[-n:]

    def detect_idle_state(self, threshold_seconds: float = 2.0) -> bool:
        """Detect if screen has been stable (idle) for threshold period.

        Args:
            threshold_seconds: Minimum seconds of stability to consider idle

        Returns:
            True if screen has been unchanged for >= threshold
        """
        if not self._last_change_time or not self._last_hash:
            return False
        return (time.time() - self._last_change_time) >= threshold_seconds

    def clear(self) -> None:
        """Clear the buffer and reset state."""
        self._buffer.clear()
        self._last_hash = ""
        self._last_change_time = 0.0
