#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Token-bucket rate limiter for WebSocket message streams."""

from __future__ import annotations

import time

#: Tightest rate (tokens/sec) any bucket-backed policy may be configured with.
#:
#: This is 1.0 for a structural reason, not a taste one: :class:`TokenBucket`
#: defaults ``burst`` to one second of the rate, so a bucket configured below
#: 1.0 can never hold a whole token and therefore denies *every* call forever,
#: however long the caller waits. A rate in ``[0, 1)`` is a bricked endpoint
#: wearing the costume of a rate limit, so the server config refuses the whole
#: band rather than accepting a number that silently means "never".
#:
#: :class:`~provide.uterm.server.bridge.hub.limiter.RateLimiter` also clamps to
#: this floor. Config refusing below it keeps the clamp from quietly handing
#: back a *looser* limit than the operator wrote.
#:
#: Making sub-1 rates meaningful would mean decoupling burst from rate
#: (``burst = max(1.0, rate)``) — a change to token-bucket semantics across
#: every port and their recorded goldens. Worth doing deliberately if a
#: sub-1 policy is ever actually wanted; not worth doing by accident here.
MIN_RATE_PER_SEC = 1.0


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
