#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for InMemoryResumeStore and ResumeSession."""

from __future__ import annotations

import time
from unittest.mock import patch

from provide.terminal.bridge.hub.resume import InMemoryResumeStore, ResumeSession, ResumeTokenStore


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
        with patch("provide.terminal.bridge.hub.resume.time") as mock_time:
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

    def test_create_token_length_is_43_chars(self) -> None:
        """Kill create__mutmut_3: nbytes 32→33 changes token length from 43 to 44."""
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 60)
        # secrets.token_urlsafe(32) → 43 base64url chars
        assert len(token) == 43

    def test_create_stored_session_has_correct_token(self) -> None:
        """Kill create__mutmut_6: session stored with token=None instead of real token."""
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 60)
        session = store.get(token)
        assert session is not None
        assert session.token == token

    def test_create_stored_session_has_created_at(self) -> None:
        """Kill create__mutmut_9: session stored with created_at=None."""
        store = InMemoryResumeStore()
        token = store.create("w1", "admin", 60)
        session = store.get(token)
        assert session is not None
        assert isinstance(session.created_at, float)
        assert session.created_at > 0.0

    def test_create_prunes_expired_tokens_opportunistically(self) -> None:
        """Creating a new token should prune previously-expired entries."""
        store = InMemoryResumeStore()
        with patch("provide.terminal.bridge.hub.resume.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            expired = store.create("w1", "admin", 1.0)
            mock_time.monotonic.return_value = 102.0
            fresh = store.create("w2", "viewer", 30.0)
            assert expired not in store.active_tokens()
            assert store.get(expired) is None
            assert store.get(fresh) is not None
            assert len(store) == 1

    def test_get_at_exact_expiry_not_expired(self) -> None:
        """Kill get__mutmut_4: > → >= makes token at exactly expires_at return None."""
        store = InMemoryResumeStore()
        with patch("provide.terminal.bridge.hub.resume.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            token = store.create("w1", "admin", 30.0)
            # At exactly expires_at (130.0), should NOT be expired (uses >, not >=)
            mock_time.monotonic.return_value = 130.0
            assert store.get(token) is not None

    def test_cleanup_expired_at_exact_expiry_not_removed(self) -> None:
        """Kill cleanup_expired__mutmut_3: > → >= removes token at exactly expires_at."""
        store = InMemoryResumeStore()
        with patch("provide.terminal.bridge.hub.resume.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            store.create("w1", "admin", 30.0)
            # At exactly expires_at (130.0), should NOT be cleaned up (uses >, not >=)
            mock_time.monotonic.return_value = 130.0
            removed = store.cleanup_expired()
            assert removed == 0
            assert len(store) == 1

    def test_active_tokens_at_exact_expiry_is_active(self) -> None:
        """Kill active_tokens__mutmut_2: <= → < excludes token at exactly expires_at."""
        store = InMemoryResumeStore()
        with patch("provide.terminal.bridge.hub.resume.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            token = store.create("w1", "admin", 30.0)
            # At exactly expires_at (130.0), should be active (uses <=, not <)
            mock_time.monotonic.return_value = 130.0
            active = store.active_tokens()
            assert token in active
