#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""StreamingDetector — catch patterns split across consecutive detect() calls.

:class:`PatternDetector` is stateless: it scans one chunk at a time, so a
multi-character pattern (an AWS key, a URL) that happens to straddle two
``detect()`` chunks is silently missed. This wrapper carries a small bounded
tail of the previous chunk and prepends it to the next one, so a boundary-split
match is still found.

It is **stateful** — use one instance per logical stream (one per session, and
not shared across event types whose text must not be concatenated). The wrapped
``PatternDetector`` stays stateless and may be shared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.annotation._detector import PatternDetector
    from provide.uterm.annotation._models import Annotation

# Longest fixed-shape secret we expect to bridge a boundary. Bounds how much of
# the previous chunk is retained (and re-scanned), capping memory and CPU.
_DEFAULT_MAX_CARRY = 512


class StreamingDetector:
    """Stateful per-stream wrapper that bridges chunk boundaries for a detector."""

    __slots__ = ("_carry", "_detector", "_max_carry")

    def __init__(self, detector: PatternDetector, *, max_carry: int = _DEFAULT_MAX_CARRY) -> None:
        self._detector = detector
        self._max_carry = max_carry
        self._carry = ""

    def detect(self, event_type: str, text: str, seq: int) -> list[Annotation]:
        """Scan *text* (joined with the carried tail) and return any matches.

        A match owned by the chunk in which it *completes* — the returned
        annotation's span carries the *seq* passed for that chunk. On a hit the
        carried tail is dropped so the same match is not re-reported on the next
        chunk; otherwise a bounded tail is kept to bridge the next boundary.
        """
        if not text:
            return []
        window = self._carry + text if self._carry else text
        annotations = self._detector.detect(event_type, window, seq)
        self._carry = "" if annotations else window[-self._max_carry :]
        return annotations

    def reset(self) -> None:
        """Forget the carried tail (e.g. on screen clear / session resync)."""
        self._carry = ""


__all__ = ["StreamingDetector"]
