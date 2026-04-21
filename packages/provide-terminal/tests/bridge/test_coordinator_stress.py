#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stress tests for HijackCoordinator contention and throughput."""

from __future__ import annotations

import time

import pytest
from provide.terminal.bridge.coordinator import HijackCoordinator


class TestCoordinatorThroughput:
    """Raw acquire/heartbeat/release cycle throughput."""

    @pytest.mark.timeout(10)
    def test_10k_acquire_release_cycles(self) -> None:
        """10k acquire→release cycles complete in <2s."""
        coord = HijackCoordinator()
        start = time.monotonic()

        for i in range(10_000):
            result = coord.acquire(f"owner-{i}", 60)
            assert result.ok
            assert result.session is not None
            rel = coord.release(result.session.hijack_id)
            assert rel.ok

        elapsed = time.monotonic() - start
        ops_per_sec = 10_000 / elapsed
        assert ops_per_sec > 5000, f"throughput {ops_per_sec:.0f} ops/s below 5000"

    @pytest.mark.timeout(10)
    def test_10k_heartbeat_cycles(self) -> None:
        """10k heartbeats on a single lease complete in <1s."""
        coord = HijackCoordinator()
        result = coord.acquire("owner", 3600)
        assert result.ok and result.session is not None
        hijack_id = result.session.hijack_id

        start = time.monotonic()
        for _ in range(10_000):
            hb = coord.heartbeat(hijack_id, 60)
            assert hb.ok

        elapsed = time.monotonic() - start
        ops_per_sec = 10_000 / elapsed
        assert ops_per_sec > 50_000, f"heartbeat throughput {ops_per_sec:.0f} ops/s below 50k"

    @pytest.mark.timeout(10)
    def test_acquire_contention_same_owner_renewal(self) -> None:
        """Same owner re-acquiring 5k times — all succeed as renewals."""
        coord = HijackCoordinator()
        for _ in range(5000):
            result = coord.acquire("alice", 60)
            assert result.ok
            assert result.session is not None

    @pytest.mark.timeout(10)
    def test_acquire_contention_different_owners_rejected(self) -> None:
        """Different owners contending — first wins, rest rejected."""
        coord = HijackCoordinator()
        result = coord.acquire("alice", 60)
        assert result.ok

        rejected = 0
        for i in range(5000):
            r = coord.acquire(f"bob-{i}", 60)
            if not r.ok:
                rejected += 1

        assert rejected == 5000

    @pytest.mark.timeout(10)
    def test_expired_lease_allows_new_acquire(self) -> None:
        """Expired leases are cleaned up, allowing new owners."""
        coord = HijackCoordinator()
        now = time.monotonic()

        # Acquire with 1s lease in the past
        result = coord.acquire("alice", 1, now=now - 10)
        assert result.ok

        # New owner succeeds because lease expired
        result2 = coord.acquire("bob", 60, now=now)
        assert result2.ok
        assert result2.session is not None
        assert result2.session.owner == "bob"

    @pytest.mark.timeout(10)
    def test_can_send_input_throughput(self) -> None:
        """100k can_send_input checks in <1s."""
        coord = HijackCoordinator()
        result = coord.acquire("owner", 3600)
        assert result.ok and result.session is not None
        hid = result.session.hijack_id

        start = time.monotonic()
        for _ in range(100_000):
            assert coord.can_send_input(hid)

        elapsed = time.monotonic() - start
        ops_per_sec = 100_000 / elapsed
        assert ops_per_sec > 500_000, f"can_send_input {ops_per_sec:.0f} ops/s below 500k"
