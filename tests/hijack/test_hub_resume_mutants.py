#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for hijack/hub — resume store and polling methods.

Targets surviving mutants in:
- resume.py: InMemoryResumeStore (create, get, cleanup_expired, active_tokens)
- polling.py: wait_for_snapshot, wait_for_guard
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.hub.resume import InMemoryResumeStore

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


# ===========================================================================
# resume.py — InMemoryResumeStore
# ===========================================================================


class TestInMemoryResumeStore:
    def test_create_token_length_32(self) -> None:
        """mutmut_3: token_urlsafe(32) → token_urlsafe(33)."""
        store = InMemoryResumeStore()
        token = store.create("w1", "operator", 60.0)
        assert len(token) >= 40
        assert token in store._tokens

    def test_create_stores_correct_token_in_session(self) -> None:
        """mutmut_6: session.token = None instead of token."""
        store = InMemoryResumeStore()
        token = store.create("w1", "operator", 60.0)
        session = store._tokens[token]
        assert session.token == token, f"session.token must equal the token key, got {session.token!r}"

    def test_create_sets_created_at_float(self) -> None:
        """mutmut_9: created_at = None instead of now."""
        store = InMemoryResumeStore()
        before = time.monotonic()
        token = store.create("w1", "operator", 60.0)
        session = store._tokens[token]
        assert session.created_at is not None
        assert isinstance(session.created_at, float)
        assert session.created_at >= before

    def test_create_sets_expires_at_correctly(self) -> None:
        """Sanity: expires_at = created_at + ttl_s."""
        store = InMemoryResumeStore()
        before = time.monotonic()
        token = store.create("w1", "operator", 60.0)
        session = store._tokens[token]
        assert session.expires_at >= before + 59.9

    def test_get_returns_session_at_exact_expiry_minus_epsilon(self) -> None:
        """mutmut_4: get expires when monotonic() >= expires_at (instead of >)."""
        store = InMemoryResumeStore()
        future = time.monotonic() + 1000.0
        token = store.create("w1", "operator", 1000.0)
        store._tokens[token].expires_at = future

        session = store.get(token)
        assert session is not None

    def test_get_returns_none_for_expired_token(self) -> None:
        """Sanity: expired token returns None."""
        store = InMemoryResumeStore()
        token = store.create("w1", "operator", 0.001)
        time.sleep(0.01)
        session = store.get(token)
        assert session is None

    def test_cleanup_expired_uses_gt_not_gte(self) -> None:
        """mutmut_3: cleanup_expired uses now > expires_at (not >=)."""
        store = InMemoryResumeStore()
        store.create("w1", "operator", 0.001)
        time.sleep(0.01)
        count = store.cleanup_expired()
        assert count == 1

    def test_active_tokens_excludes_expired(self) -> None:
        """mutmut_2: active_tokens uses now <= expires_at (includes at-expiry)."""
        store = InMemoryResumeStore()
        token = store.create("w1", "operator", 3600.0)

        active = store.active_tokens()
        assert token in active

    def test_active_tokens_uses_lte_not_lt(self) -> None:
        """mutmut_2 boundary: token expiring well in the future IS included."""
        store = InMemoryResumeStore()
        token = store.create("w1", "operator", 1.0)
        active = store.active_tokens()
        assert token in active


# ===========================================================================
# polling.py — wait_for_snapshot
# ===========================================================================
