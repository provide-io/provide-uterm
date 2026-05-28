#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""RateLimiter: per-purpose token-bucket policy composer for hub REST endpoints.

This module is the *policy* surface that composes
:class:`provide.uterm.server.bridge.ratelimit.TokenBucket` primitives into a
coherent set of named limits used by the hub's REST routes.

Two purposes are tracked today:

- REST hijack-acquire (``allow_rest_acquire``) — gates ``POST /acquire``
  style flows. Global bucket + per-client bucket; both must allow.
- REST send (``allow_rest_send``) — gates ``POST /send`` style flows.
  Same global + per-client composition with its own tunables.

Per-client buckets are stored in plain dicts capped at
:data:`REST_CLIENT_CACHE_MAX`. On overflow the oldest half of entries
are evicted (LRU-lite) so recently-active clients keep their
rate-limit state while bounding memory growth — this is the same
strategy that previously lived inline in ``_ConnectionMixin``.

Lock semantics: the limiter holds no locks. Concurrent calls are safe
because CPython dict ops are atomic and ``TokenBucket.allow`` is a
short, side-effecting computation on private float state. The hub's
``_lock`` continues to coordinate higher-level invariants where
needed.
"""

from __future__ import annotations

from provide.uterm.server.bridge.ratelimit import TokenBucket

# Maximum number of per-client rate-limit buckets held in memory at once.
# On overflow the oldest half of entries are evicted (LRU-lite), preserving
# rate-limit state for recently-active clients while bounding memory growth.
REST_CLIENT_CACHE_MAX = 1024
REST_CLIENT_EVICT_COUNT = REST_CLIENT_CACHE_MAX // 2


class RateLimiter:
    """Coordinates per-purpose token buckets for hub REST endpoints.

    Composes :class:`TokenBucket` primitives into named policies:

    - REST hijack-acquire (global + per-client)
    - REST send (global + per-client)

    The class is intentionally side-effect free apart from mutating its
    own bucket state; logging of rate-limit hits remains the caller's
    responsibility so the hub keeps a single structured-event surface.

    Args:
        rest_acquire_rate: Tokens/sec for the REST acquire policy
            (both global bucket and freshly-minted per-client buckets).
        rest_send_rate: Tokens/sec for the REST send policy.
    """

    __slots__ = (
        "_rest_acquire_bucket",
        "_rest_acquire_per_client",
        "_rest_acquire_rate",
        "_rest_send_bucket",
        "_rest_send_per_client",
        "_rest_send_rate",
    )

    def __init__(self, rest_acquire_rate: float, rest_send_rate: float) -> None:
        self._rest_acquire_rate = max(0.1, float(rest_acquire_rate))
        self._rest_send_rate = max(0.1, float(rest_send_rate))
        self._rest_acquire_bucket = TokenBucket(self._rest_acquire_rate)
        self._rest_send_bucket = TokenBucket(self._rest_send_rate)
        self._rest_acquire_per_client: dict[str, TokenBucket] = {}
        self._rest_send_per_client: dict[str, TokenBucket] = {}

    # -- Properties exposing the underlying primitives ---------------------
    # These are deliberately mutable: legacy hub tests still poke buckets
    # directly (e.g. force ``_tokens = 0`` to simulate exhaustion) and the
    # hub exposes the limiter via property shims for back-compat.

    @property
    def rest_acquire_bucket(self) -> TokenBucket:
        """Global REST acquire bucket."""
        return self._rest_acquire_bucket

    @rest_acquire_bucket.setter
    def rest_acquire_bucket(self, bucket: TokenBucket) -> None:
        self._rest_acquire_bucket = bucket

    @property
    def rest_send_bucket(self) -> TokenBucket:
        """Global REST send bucket."""
        return self._rest_send_bucket

    @rest_send_bucket.setter
    def rest_send_bucket(self, bucket: TokenBucket) -> None:
        self._rest_send_bucket = bucket

    @property
    def rest_acquire_per_client(self) -> dict[str, TokenBucket]:
        """Per-client REST acquire buckets (mutable view)."""
        return self._rest_acquire_per_client

    @rest_acquire_per_client.setter
    def rest_acquire_per_client(self, value: dict[str, TokenBucket]) -> None:
        self._rest_acquire_per_client = value

    @property
    def rest_send_per_client(self) -> dict[str, TokenBucket]:
        """Per-client REST send buckets (mutable view)."""
        return self._rest_send_per_client

    @rest_send_per_client.setter
    def rest_send_per_client(self, value: dict[str, TokenBucket]) -> None:
        self._rest_send_per_client = value

    @property
    def rest_acquire_rate(self) -> float:
        """Configured tokens/sec for the REST acquire policy."""
        return self._rest_acquire_rate

    @property
    def rest_send_rate(self) -> float:
        """Configured tokens/sec for the REST send policy."""
        return self._rest_send_rate

    # -- Public policy surface ---------------------------------------------

    def allow_rest_acquire(self, client_id: str) -> bool:
        """Return True if *client_id* passes both global and per-client acquire limits.

        The per-client dict is capped at :data:`REST_CLIENT_CACHE_MAX`;
        on overflow the oldest half of entries are evicted (LRU-lite).
        Both the per-client bucket and the global bucket must allow for
        the request to be admitted — short-circuit semantics are
        preserved (the per-client bucket is consumed first; if it
        denies, the global bucket is *not* consumed).
        """
        self._evict_if_full(self._rest_acquire_per_client)
        bucket = self._rest_acquire_per_client.setdefault(client_id, TokenBucket(self._rest_acquire_rate))
        return bucket.allow() and self._rest_acquire_bucket.allow()

    def allow_rest_send(self, client_id: str) -> bool:
        """Return True if *client_id* passes both global and per-client send limits.

        Same composition and eviction strategy as
        :meth:`allow_rest_acquire`; uses its own pair of buckets so the
        REST acquire and send rate ceilings can be tuned independently.
        """
        self._evict_if_full(self._rest_send_per_client)
        bucket = self._rest_send_per_client.setdefault(client_id, TokenBucket(self._rest_send_rate))
        return bucket.allow() and self._rest_send_bucket.allow()

    # -- Internal helpers --------------------------------------------------

    @staticmethod
    def _evict_if_full(per_client: dict[str, TokenBucket]) -> None:
        """Drop the oldest half of *per_client* if it has reached the cap.

        Insertion order is preserved by CPython dicts, so iterating the
        keys and trimming the first ``REST_CLIENT_EVICT_COUNT`` entries
        approximates an LRU eviction without the overhead of a real LRU
        cache. The eviction count is half the cap so the next overflow
        is amortised across many calls.
        """
        if len(per_client) >= REST_CLIENT_CACHE_MAX:
            for k in list(per_client)[:REST_CLIENT_EVICT_COUNT]:
                del per_client[k]


__all__ = ["REST_CLIENT_CACHE_MAX", "REST_CLIENT_EVICT_COUNT", "RateLimiter"]
