#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the :class:`RateLimiter` policy composer.

These cover the new service-class surface directly (``allow_rest_acquire``
and ``allow_rest_send``) plus the LRU-lite eviction semantics that were
previously inlined in ``_ConnectionMixin``. The hub itself still has
property shims onto the underlying buckets, so the legacy integration
tests in ``tests/bridge/test_connections_*`` continue to exercise the
shim path; this file is the canonical unit-test for the extracted
service.
"""

from __future__ import annotations

from provide.uterm.server.bridge.hub.limiter import (
    REST_CLIENT_CACHE_MAX,
    REST_CLIENT_EVICT_COUNT,
    RateLimiter,
)
from provide.uterm.server.bridge.ratelimit import TokenBucket


def _drain(bucket: TokenBucket) -> None:
    """Force a bucket to be empty so the next ``allow()`` returns False."""
    bucket._tokens = 0.0


def test_init_clamps_rates_to_minimum() -> None:
    """Rates below 0.1/sec are clamped — protects against divide-by-zero and stuck buckets."""
    limiter = RateLimiter(rest_acquire_rate=0.0, rest_send_rate=-5.0)
    assert limiter.rest_acquire_rate == 0.1
    assert limiter.rest_send_rate == 0.1


def test_init_preserves_normal_rates() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    assert limiter.rest_acquire_rate == 5.0
    assert limiter.rest_send_rate == 20.0
    assert isinstance(limiter.rest_acquire_bucket, TokenBucket)
    assert isinstance(limiter.rest_send_bucket, TokenBucket)
    assert limiter.rest_acquire_per_client == {}
    assert limiter.rest_send_per_client == {}


def test_allow_rest_acquire_admits_first_request() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    assert limiter.allow_rest_acquire("client-a") is True
    # The per-client dict has gained an entry for the new client.
    assert "client-a" in limiter.rest_acquire_per_client


def test_allow_rest_send_admits_first_request() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    assert limiter.allow_rest_send("client-a") is True
    assert "client-a" in limiter.rest_send_per_client


def test_allow_rest_acquire_per_client_isolation() -> None:
    """Exhausting one client's bucket must not affect another client."""
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    # First call creates client-a's per-client bucket and consumes from it.
    limiter.allow_rest_acquire("client-a")
    _drain(limiter.rest_acquire_per_client["client-a"])
    assert limiter.allow_rest_acquire("client-a") is False
    # client-b gets its own bucket; should be admitted.
    assert limiter.allow_rest_acquire("client-b") is True
    assert limiter.rest_acquire_per_client["client-a"] is not limiter.rest_acquire_per_client["client-b"]


def test_allow_rest_send_per_client_isolation() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    limiter.allow_rest_send("c1")
    _drain(limiter.rest_send_per_client["c1"])
    assert limiter.allow_rest_send("c1") is False
    assert limiter.allow_rest_send("c2") is True


def test_allow_rest_acquire_global_bucket_denies_after_per_client_pass() -> None:
    """Per-client passes but the global bucket is empty — request must be rejected."""
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    _drain(limiter.rest_acquire_bucket)
    assert limiter.allow_rest_acquire("client-a") is False


def test_allow_rest_send_global_bucket_denies_after_per_client_pass() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    _drain(limiter.rest_send_bucket)
    assert limiter.allow_rest_send("client-a") is False


def test_allow_rest_acquire_short_circuit_skips_global_when_per_client_denies() -> None:
    """If the per-client bucket denies, the global bucket must not be consumed."""
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    # Pre-create a drained per-client bucket so allow() returns False without
    # touching the global bucket.
    limiter.rest_acquire_per_client["client-a"] = TokenBucket(5.0)
    _drain(limiter.rest_acquire_per_client["client-a"])
    global_before = limiter.rest_acquire_bucket._tokens
    assert limiter.allow_rest_acquire("client-a") is False
    assert limiter.rest_acquire_bucket._tokens == global_before


def test_allow_rest_send_short_circuit_skips_global_when_per_client_denies() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    limiter.rest_send_per_client["client-a"] = TokenBucket(20.0)
    _drain(limiter.rest_send_per_client["client-a"])
    global_before = limiter.rest_send_bucket._tokens
    assert limiter.allow_rest_send("client-a") is False
    assert limiter.rest_send_bucket._tokens == global_before


def test_acquire_eviction_at_cap_drops_oldest_half() -> None:
    """Filling the per-client cache triggers LRU-lite eviction of the oldest half."""
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    # Pre-populate to the cap with dummy buckets so we can observe eviction
    # without burning real rate budget.
    limiter.rest_acquire_per_client = {f"c{i}": TokenBucket(5.0) for i in range(REST_CLIENT_CACHE_MAX)}
    assert len(limiter.rest_acquire_per_client) == REST_CLIENT_CACHE_MAX
    # One more client triggers eviction of the oldest REST_CLIENT_EVICT_COUNT
    # entries before the new bucket is inserted.
    limiter.allow_rest_acquire("new-client")
    expected = REST_CLIENT_CACHE_MAX - REST_CLIENT_EVICT_COUNT + 1
    assert len(limiter.rest_acquire_per_client) == expected
    assert "new-client" in limiter.rest_acquire_per_client
    assert "c0" not in limiter.rest_acquire_per_client  # oldest evicted
    assert f"c{REST_CLIENT_CACHE_MAX - 1}" in limiter.rest_acquire_per_client  # newest kept


def test_send_eviction_at_cap_drops_oldest_half() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    limiter.rest_send_per_client = {f"c{i}": TokenBucket(20.0) for i in range(REST_CLIENT_CACHE_MAX)}
    limiter.allow_rest_send("new-client")
    expected = REST_CLIENT_CACHE_MAX - REST_CLIENT_EVICT_COUNT + 1
    assert len(limiter.rest_send_per_client) == expected
    assert "new-client" in limiter.rest_send_per_client
    assert "c0" not in limiter.rest_send_per_client


def test_bucket_setters_replace_underlying_state() -> None:
    """Tests still need to swap in mocks; the setter shims must update state."""
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    new_acquire = TokenBucket(99.0)
    new_send = TokenBucket(77.0)
    limiter.rest_acquire_bucket = new_acquire
    limiter.rest_send_bucket = new_send
    assert limiter.rest_acquire_bucket is new_acquire
    assert limiter.rest_send_bucket is new_send


def test_per_client_setters_replace_underlying_dict() -> None:
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    replacement_acquire = {"x": TokenBucket(5.0)}
    replacement_send = {"y": TokenBucket(20.0)}
    limiter.rest_acquire_per_client = replacement_acquire
    limiter.rest_send_per_client = replacement_send
    assert limiter.rest_acquire_per_client is replacement_acquire
    assert limiter.rest_send_per_client is replacement_send


# ---------------------------------------------------------------------------
# SRV-rl: eviction is true LRU and never resets the active (inserting) client.
# ---------------------------------------------------------------------------


def test_acquire_eviction_never_drops_the_inserting_client() -> None:
    """A client that overflows the cache must NOT have its own bucket reset.

    Old bug: eviction ran *before* setdefault, evicting the first
    REST_CLIENT_EVICT_COUNT keys by insertion order. A client whose bucket sat
    in that window — and which then triggers the overflow insert itself — would
    have its drained bucket evicted and immediately recreated full, resetting
    its own limit. Eviction must run *after* the insert and never drop the key
    just inserted.
    """
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    # The victim is the OLDEST key (front of the evict window) with a drained
    # bucket. It must survive when it is the one forcing the overflow.
    victim = "c0"
    pre = {f"c{i}": TokenBucket(5.0) for i in range(REST_CLIENT_CACHE_MAX)}
    _drain(pre[victim])
    limiter.rest_acquire_per_client = pre

    # The victim itself forces the overflow. Its drained bucket must be kept,
    # so the call is rejected — its limit is NOT reset.
    assert limiter.allow_rest_acquire(victim) is False
    assert victim in limiter.rest_acquire_per_client
    assert limiter.rest_acquire_per_client[victim]._tokens < 1.0


def test_send_eviction_never_drops_the_inserting_client() -> None:
    """Same evict-after-insert guarantee for the send limiter."""
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    victim = "c0"
    pre = {f"c{i}": TokenBucket(20.0) for i in range(REST_CLIENT_CACHE_MAX)}
    _drain(pre[victim])
    limiter.rest_send_per_client = pre

    assert limiter.allow_rest_send(victim) is False
    assert victim in limiter.rest_send_per_client


def test_acquire_access_moves_client_to_end_lru() -> None:
    """Accessing an existing client refreshes its recency (true LRU).

    Old code was FIFO: a frequently-used client inserted early could still be
    evicted. After this fix, touching a client moves it to the most-recent
    position, so the oldest *untouched* clients are evicted first.
    """
    limiter = RateLimiter(rest_acquire_rate=5.0, rest_send_rate=20.0)
    pre = {f"c{i}": TokenBucket(5.0) for i in range(REST_CLIENT_CACHE_MAX)}
    limiter.rest_acquire_per_client = pre

    # Touch c0 (the oldest) so it becomes the most-recently-used; it must
    # survive the next overflow eviction while c1 (now oldest) is dropped.
    limiter.allow_rest_acquire("c0")
    limiter.allow_rest_acquire("new-client")

    assert "c0" in limiter.rest_acquire_per_client  # refreshed → kept
    assert "c1" not in limiter.rest_acquire_per_client  # now-oldest → evicted
    assert "new-client" in limiter.rest_acquire_per_client


def test_evict_if_full_protects_keep_key_inside_eviction_window() -> None:
    """``_evict_if_full`` never drops *keep*, even when it sits inside the
    oldest-half eviction window.

    Via the public API ``_touch`` always moves *keep* to the end first, so this
    defensive branch is exercised directly here (keep = the oldest key)."""
    per_client = {f"c{i}": TokenBucket(5.0) for i in range(REST_CLIENT_CACHE_MAX + 1)}
    keep = "c0"  # oldest key — inside the first REST_CLIENT_EVICT_COUNT entries

    RateLimiter._evict_if_full(per_client, keep=keep)

    assert keep in per_client  # protected despite being in the eviction window
    assert "c1" not in per_client  # a non-keep key in the window was evicted
