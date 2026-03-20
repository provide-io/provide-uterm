#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for InMemoryResumeStore and ResumeSession."""

from __future__ import annotations

import time
from unittest.mock import patch

from provide.terminal.hijack.hub.resume import InMemoryResumeStore, ResumeSession, ResumeTokenStore


class TestResumeSession:
    def test_dataclass_fields(self) -> None:
        s = ResumeSession(
            token="abc",
            worker_id="w1",
            role="admin",
            created_at=1.0,
            expires_at=2.0,
        )
        assert s.token == "abc"
        assert s.worker_id == "w1"
        assert s.role == "admin"
        assert s.was_hijack_owner is False

    def test_was_hijack_owner_default(self) -> None:
        s = ResumeSession(token="t", worker_id="w", role="viewer", created_at=0, expires_at=1)
        assert s.was_hijack_owner is False

    def test_was_hijack_owner_set(self) -> None:
        s = ResumeSession(token="t", worker_id="w", role="admin", created_at=0, expires_at=1, was_hijack_owner=True)
        assert s.was_hijack_owner is True


class TestInMemoryResumeStore:
    def test_create_returns_unique_tokens(self) -> None:
        store = InMemoryResumeStore()
        t1 = store.create("w1", "admin", 60)
        t2 = store.create("w1", "admin", 60)
        assert t1 != t2
        assert len(store) == 2

    def test_get_returns_session(self) -> None:
        store = InMemoryResumeStore()
        token = store.create("w1", "operator", 60)
        session = store.get(token)
        assert session is not None
        assert session.worker_id == "w1"
        assert session.role == "operator"
        assert session.was_hijack_owner is False

    def test_get_nonexistent_returns_none(self) -> None:
        store = InMemoryResumeStore()
        assert store.get("nonexistent") is None

    def test_get_expired_returns_none_and_removes(self) -> None:
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 0.001)
        # Wait for expiry
        time.sleep(0.01)
        assert store.get(token) is None
        assert len(store) == 0

    def test_revoke_removes_token(self) -> None:
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 60)
        store.revoke(token)
        assert store.get(token) is None
        assert len(store) == 0

    def test_revoke_nonexistent_is_noop(self) -> None:
        store = InMemoryResumeStore()
        store.revoke("nonexistent")  # should not raise

    def test_mark_hijack_owner(self) -> None:
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 60)
        store.mark_hijack_owner(token, True)
        session = store.get(token)
        assert session is not None
        assert session.was_hijack_owner is True

    def test_mark_hijack_owner_false(self) -> None:
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 60)
        store.mark_hijack_owner(token, True)
        store.mark_hijack_owner(token, False)
        session = store.get(token)
        assert session is not None
        assert session.was_hijack_owner is False

    def test_mark_hijack_owner_nonexistent_is_noop(self) -> None:
        store = InMemoryResumeStore()
        store.mark_hijack_owner("nonexistent", True)  # should not raise

    def test_cleanup_expired(self) -> None:
        store = InMemoryResumeStore()
        store.create("w1", "admin", 0.001)
        store.create("w2", "viewer", 60)
        time.sleep(0.01)
        removed = store.cleanup_expired()
        assert removed == 1
        assert len(store) == 1

    def test_cleanup_expired_none_expired(self) -> None:
        store = InMemoryResumeStore()
        store.create("w1", "admin", 60)
        assert store.cleanup_expired() == 0
        assert len(store) == 1

    def test_active_tokens(self) -> None:
        store = InMemoryResumeStore()
        t1 = store.create("w1", "admin", 60)
        t2 = store.create("w2", "viewer", 0.001)
        time.sleep(0.01)
        active = store.active_tokens()
        assert t1 in active
        assert t2 not in active

    def test_protocol_conformance(self) -> None:
        store = InMemoryResumeStore()
        assert isinstance(store, ResumeTokenStore)

    def test_ttl_respected(self) -> None:
        store = InMemoryResumeStore()
        with patch("provide.terminal.hijack.hub.resume.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            token = store.create("w1", "admin", 30)
            # Not expired yet at 129
            mock_time.monotonic.return_value = 129.0
            assert store.get(token) is not None
            # Expired at 131
            mock_time.monotonic.return_value = 131.0
            assert store.get(token) is None

    def test_multiple_workers(self) -> None:
        store = InMemoryResumeStore()
        t1 = store.create("w1", "admin", 60)
        t2 = store.create("w2", "operator", 60)
        s1 = store.get(t1)
        s2 = store.get(t2)
        assert s1 is not None and s1.worker_id == "w1"
        assert s2 is not None and s2.worker_id == "w2"
