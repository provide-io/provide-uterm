#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Public terminal-frame lifecycle signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TerminalFrame:
    """One terminal update with its correlated, owned screen snapshot."""

    sequence: int
    snapshot: dict[str, Any]
    transcript_delta: str

    @property
    def cursor(self) -> dict[str, Any]:
        """Return an owned copy of the frame's cursor position."""
        cursor = self.snapshot.get("cursor")
        return dict(cursor) if isinstance(cursor, dict) else {"x": 0, "y": 0}


class TerminalFrameDisconnectedError(ConnectionError):
    """Signal that a terminal-frame wait ended because the transport closed."""


__all__ = ["TerminalFrame", "TerminalFrameDisconnectedError"]
