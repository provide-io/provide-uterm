#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stress tests for DeckMux PresenceStore at scale."""

from __future__ import annotations

import time

import pytest

from provide.terminal.deckmux._presence import PresenceStore

COLORS = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c"]


class TestPresenceStoreScale:
    """PresenceStore under high user count and update frequency."""

    @pytest.mark.timeout(10)
    def test_200_concurrent_users(self) -> None:
        """200 users in a single session — add, get_all, remove."""
        store = PresenceStore()
        for i in range(200):
            store.add(f"user-{i}", f"User {i}", COLORS[i % len(COLORS)], "viewer")

        all_users = store.get_all()
        assert len(all_users) == 200

        for i in range(200):
            store.remove(f"user-{i}")
        assert len(store.get_all()) == 0

    @pytest.mark.timeout(10)
    def test_50k_scroll_updates_throughput(self) -> None:
        """50k scroll updates across 100 users — >100k ops/sec."""
        store = PresenceStore()
        for i in range(100):
            store.add(f"user-{i}", f"User {i}", COLORS[i % len(COLORS)], "viewer")

        start = time.monotonic()
        for i in range(50_000):
            uid = f"user-{i % 100}"
            line = i % 500
            store.update(uid, scroll_line=line, scroll_range=(line, line + 24))

        elapsed = time.monotonic() - start
        ops_per_sec = 50_000 / elapsed
        assert ops_per_sec > 100_000, f"scroll update throughput {ops_per_sec:.0f} ops/s below 100k"

    @pytest.mark.timeout(10)
    def test_10k_add_remove_churn(self) -> None:
        """10k user join/leave cycles — no memory growth or crash."""
        store = PresenceStore()
        for i in range(10_000):
            uid = f"churn-{i}"
            store.add(uid, f"User {i}", "#fff", "viewer")
            store.remove(uid)

        assert len(store.get_all()) == 0

    @pytest.mark.timeout(10)
    def test_to_dict_serialization_at_scale(self) -> None:
        """Serialize 100 users to dicts — >50k serializations/sec."""
        store = PresenceStore()
        for i in range(100):
            p = store.add(f"user-{i}", f"User {i}", COLORS[i % len(COLORS)], "viewer")
            p.scroll_line = i * 10
            p.typing = i % 3 == 0

        start = time.monotonic()
        for _ in range(5000):
            for p in store.get_all():
                p.to_dict()

        elapsed = time.monotonic() - start
        total_ops = 5000 * 100
        ops_per_sec = total_ops / elapsed
        assert ops_per_sec > 50_000, f"to_dict throughput {ops_per_sec:.0f} ops/s below 50k"

    @pytest.mark.timeout(10)
    def test_set_owner_transfer_at_scale(self) -> None:
        """1000 owner transfers across 100 users — all consistent."""
        store = PresenceStore()
        for i in range(100):
            store.add(f"user-{i}", f"User {i}", "#fff", "viewer")

        for i in range(1000):
            store.set_owner(f"user-{i % 100}")
            owner = store.get_owner()
            assert owner is not None
            assert owner.user_id == f"user-{i % 100}"

    @pytest.mark.timeout(10)
    def test_idle_detection_at_scale(self) -> None:
        """Check idle status across 200 users — fast scan."""
        store = PresenceStore()
        for i in range(200):
            p = store.add(f"user-{i}", f"User {i}", "#fff", "viewer")
            # Half the users are "old" (idle)
            if i % 2 == 0:
                p.last_activity_at = time.time() - 600

        idle_count = sum(1 for p in store.get_all() if p.is_idle(300))
        assert idle_count == 100
