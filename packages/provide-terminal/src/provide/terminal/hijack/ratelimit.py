#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Token-bucket rate limiter for WebSocket message streams."""

from __future__ import annotations

import time


class TokenBucket:
    """Simple token-bucket rate limiter.

    Args:
        rate_per_sec: Sustained token refill rate (tokens per second).
        burst: Maximum burst size.  Defaults to ``rate_per_sec`` (one second
            of burst capacity).
    """

    __slots__ = ("_burst", "_last_refill", "_rate", "_tokens")

    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self._rate = float(rate_per_sec)
        self._burst = float(burst if burst is not None else rate_per_sec)
        self._tokens = self._burst
        self._last_refill = time.monotonic()

    def allow(self) -> bool:
        """Consume one token if available. Returns ``True`` if allowed."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False
