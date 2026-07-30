#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``ratelimit`` port.

The bucket reads a monotonic clock, so the corpus drives it with a stubbed
clock rather than real time: a rate limiter asserted against wall time is a
flaky test, and the refill arithmetic is exactly what needs pinning.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_ratelimit_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.server.bridge.hub.limiter import (
    REST_CLIENT_CACHE_MAX,
    REST_CLIENT_EVICT_COUNT,
    RateLimiter,
)
from provide.uterm.server.bridge.ratelimit import TokenBucket

OUT = Path(__file__).with_name("ratelimit_golden.json")

# (name, rate, burst, [(advance_seconds, calls), ...])
BUCKET_CASES: list[tuple[str, float, float | None, list[tuple[float, int]]]] = [
    ("burst defaults to the rate", 3.0, None, [(0.0, 5)]),
    ("explicit burst", 1.0, 5.0, [(0.0, 7)]),
    ("refills at the configured rate", 1.0, 1.0, [(0.0, 2), (1.0, 2), (1.0, 2)]),
    ("partial refill does not grant a token", 1.0, 1.0, [(0.0, 1), (0.5, 1), (0.5, 1)]),
    ("refill is capped at the burst", 2.0, 2.0, [(0.0, 2), (100.0, 5)]),
    ("fractional rate", 0.5, 1.0, [(0.0, 1), (1.0, 1), (1.0, 1)]),
    ("zero rate never refills", 0.0, 1.0, [(0.0, 2), (10.0, 1)]),
    ("high rate", 100.0, 100.0, [(0.0, 101)]),
]


def _drive_bucket(rate: float, burst: float | None, script: list[tuple[float, int]]) -> list[bool]:
    """Run one bucket through a scripted clock and record every verdict."""
    now = 1000.0
    with mock.patch("time.monotonic", side_effect=lambda: now):
        bucket = TokenBucket(rate, burst)
        verdicts: list[bool] = []
        for advance, calls in script:
            now += advance
            for _ in range(calls):
                verdicts.append(bucket.allow())
        return verdicts


def _limiter_record() -> dict[str, Any]:
    """Global and per-client composition, and the LRU eviction rule."""
    now = 1000.0
    with mock.patch("time.monotonic", side_effect=lambda: now):
        # A generous global bucket with a tight per-client one: the per-client
        # limit is what should bite.
        limiter = RateLimiter(rest_acquire_rate=2.0, rest_send_rate=2.0)
        limiter.rest_acquire_bucket = TokenBucket(1000.0)
        limiter.rest_send_bucket = TokenBucket(1000.0)
        per_client = [limiter.allow_rest_acquire("c1") for _ in range(4)]
        other_client = [limiter.allow_rest_acquire("c2") for _ in range(4)]
        send = [limiter.allow_rest_send("c1") for _ in range(4)]

        # A tight global bucket denies even a fresh client.
        global_limiter = RateLimiter(rest_acquire_rate=1000.0, rest_send_rate=1000.0)
        global_limiter.rest_acquire_bucket = TokenBucket(2.0)
        global_verdicts = [global_limiter.allow_rest_acquire(f"c{i}") for i in range(4)]

        # A denying per-client bucket must not consume a global token.
        short_circuit = RateLimiter(rest_acquire_rate=1.0, rest_send_rate=1.0)
        short_circuit.rest_acquire_bucket = TokenBucket(1000.0)
        short_circuit.allow_rest_acquire("c1")
        short_circuit.allow_rest_acquire("c1")
        global_tokens_after_denial = short_circuit.rest_acquire_bucket._tokens

    return {
        "per_client": per_client,
        "other_client": other_client,
        "send": send,
        "global_verdicts": global_verdicts,
        "global_untouched_by_denial": global_tokens_after_denial >= 999.0,
    }


def _eviction_record() -> dict[str, Any]:
    """Overflow evicts the oldest half and never the client just served."""
    now = 1000.0
    with mock.patch("time.monotonic", side_effect=lambda: now):
        limiter = RateLimiter(rest_acquire_rate=1000.0, rest_send_rate=1000.0)
        for i in range(REST_CLIENT_CACHE_MAX):
            limiter.allow_rest_acquire(f"c{i}")
        at_cap = len(limiter.rest_acquire_per_client)
        limiter.allow_rest_acquire("overflow")
        after = limiter.rest_acquire_per_client
        return {
            "cache_max": REST_CLIENT_CACHE_MAX,
            "evict_count": REST_CLIENT_EVICT_COUNT,
            "size_at_cap": at_cap,
            "size_after_overflow": len(after),
            "overflow_client_kept": "overflow" in after,
            "oldest_evicted": "c0" not in after,
            "newest_kept": f"c{REST_CLIENT_CACHE_MAX - 1}" in after,
        }


def _eviction_small_record() -> dict[str, Any]:
    """Same eviction rule as `_eviction_record`, at a scale small enough for a
    mutation-testing tool's hit-count budget to exercise the loop's early-exit
    without tripping a runaway-loop safety valve. See ratelimit.ts's
    evictIfFull docstring.
    """
    now = 1000.0
    with (
        mock.patch("time.monotonic", side_effect=lambda: now),
        mock.patch("provide.uterm.server.bridge.hub.limiter.REST_CLIENT_CACHE_MAX", 8),
        mock.patch("provide.uterm.server.bridge.hub.limiter.REST_CLIENT_EVICT_COUNT", 4),
    ):
        limiter = RateLimiter(rest_acquire_rate=1000.0, rest_send_rate=1000.0)
        for i in range(8):
            limiter.allow_rest_acquire(f"c{i}")
        at_cap = len(limiter.rest_acquire_per_client)
        limiter.allow_rest_acquire("overflow")
        after = limiter.rest_acquire_per_client
        return {
            "cache_max": 8,
            "evict_count": 4,
            "size_at_cap": at_cap,
            "size_after_overflow": len(after),
            "overflow_client_kept": "overflow" in after,
            "oldest_evicted": "c0" not in after,
            "newest_kept": "c7" in after,
        }


def _self_reset_record() -> dict[str, Any]:
    """A client churning the cache must not be able to reset its own limit."""
    now = 1000.0
    with mock.patch("time.monotonic", side_effect=lambda: now):
        limiter = RateLimiter(rest_acquire_rate=1.0, rest_send_rate=1.0)
        limiter.rest_acquire_bucket = TokenBucket(1000.0)
        first = limiter.allow_rest_acquire("victim")
        second = limiter.allow_rest_acquire("victim")
        # Churn the cache past the cap, then come back.
        for i in range(REST_CLIENT_CACHE_MAX + 1):
            limiter.allow_rest_acquire(f"noise{i}")
        after_churn = limiter.allow_rest_acquire("victim")
    return {"first": first, "second": second, "after_churn": after_churn}


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_ratelimit_golden.py",
        "buckets": [
            {
                "name": name,
                "rate": rate,
                "burst": burst,
                "script": script,
                "verdicts": _drive_bucket(rate, burst, script),
            }
            for (name, rate, burst, script) in BUCKET_CASES
        ],
        "limiter": _limiter_record(),
        "eviction": _eviction_record(),
        "eviction_small": _eviction_small_record(),
        "self_reset": _self_reset_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['buckets'])} bucket cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
