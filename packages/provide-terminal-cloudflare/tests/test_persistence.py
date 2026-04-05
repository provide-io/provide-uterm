#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for do/persistence.py — persist_lease and clear_lease module-level functions."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

from provide.terminal.cloudflare.bridge.hijack import HijackSession
from provide.terminal.cloudflare.do.persistence import clear_lease, persist_lease
from provide.terminal.cloudflare.state.store import LeaseRecord, SqliteStateStore


def _make_store() -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


def _make_ctx_with_alarm() -> tuple[SimpleNamespace, list[int]]:
    """Return a ctx with a setAlarm spy and the recorded alarm values."""
    alarm_calls: list[int] = []
    ctx = SimpleNamespace(
        storage=SimpleNamespace(setAlarm=lambda ms: alarm_calls.append(ms)),
    )
    return ctx, alarm_calls


def _make_session(lease_s: float = 60.0) -> HijackSession:
    return HijackSession(
        hijack_id="test-hid",
        owner="test-owner",
        lease_expires_at=time.time() + lease_s,
    )


# ---------------------------------------------------------------------------
# persist_lease — happy paths
# ---------------------------------------------------------------------------


class TestPersistLease:
    def test_persist_lease_saves_to_store(self) -> None:
        """persist_lease writes a LeaseRecord to the store."""
        store = _make_store()
        ctx = SimpleNamespace(storage=SimpleNamespace(setAlarm=lambda ms: None))
        session = _make_session()

        persist_lease(store, ctx, "w1", session, LeaseRecord)

        row = store.load_session("w1")
        assert row is not None, "Session should be saved after persist_lease"
        assert row.get("hijack_id") == "test-hid", f"Unexpected hijack_id: {row.get('hijack_id')}"
        assert row.get("owner") == "test-owner", f"Unexpected owner: {row.get('owner')}"

    def test_persist_lease_schedules_alarm(self) -> None:
        """persist_lease calls ctx.storage.setAlarm with the expiry time in ms."""
        store = _make_store()
        ctx, alarm_calls = _make_ctx_with_alarm()
        session = _make_session(lease_s=30.0)

        persist_lease(store, ctx, "w1", session, LeaseRecord)

        assert len(alarm_calls) == 1, f"Expected 1 alarm call, got {len(alarm_calls)}"
        expected_ms = int(session.lease_expires_at * 1000)
        assert alarm_calls[0] == expected_ms, f"Alarm should be set to {expected_ms}, got {alarm_calls[0]}"

    def test_persist_lease_noop_when_session_is_none(self) -> None:
        """persist_lease does nothing when session is None."""
        store = _make_store()
        ctx, alarm_calls = _make_ctx_with_alarm()

        persist_lease(store, ctx, "w1", None, LeaseRecord)

        assert len(alarm_calls) == 0, "No alarm should be set when session is None"
        row = store.load_session("w1")
        assert row is None, "No session should be saved when session is None"

    def test_persist_lease_no_alarm_when_storage_missing(self) -> None:
        """persist_lease handles ctx without storage gracefully."""
        store = _make_store()
        ctx = SimpleNamespace()  # no storage attribute
        session = _make_session()

        # Should not raise
        persist_lease(store, ctx, "w1", session, LeaseRecord)

        row = store.load_session("w1")
        assert row is not None, "Lease should still be saved even without ctx.storage"

    def test_persist_lease_no_alarm_when_set_alarm_missing(self) -> None:
        """persist_lease handles storage without setAlarm attribute."""
        store = _make_store()
        ctx = SimpleNamespace(storage=SimpleNamespace())  # no setAlarm
        session = _make_session()

        # Should not raise
        persist_lease(store, ctx, "w1", session, LeaseRecord)

        row = store.load_session("w1")
        assert row is not None, "Lease should still be saved when setAlarm is absent"


# ---------------------------------------------------------------------------
# clear_lease — happy paths
# ---------------------------------------------------------------------------


class TestClearLease:
    def test_clear_lease_removes_persisted_lease(self) -> None:
        """clear_lease removes the lease record from the store."""
        store = _make_store()
        ctx = SimpleNamespace(storage=SimpleNamespace(setAlarm=lambda ms: None))
        session = _make_session()

        persist_lease(store, ctx, "w1", session, LeaseRecord)

        # Verify it was saved
        assert store.load_session("w1") is not None, "Lease should be saved before clear"

        clear_lease(store, "w1")

        row = store.load_session("w1")
        # After clearing, hijack_id should be gone
        assert row is None or row.get("hijack_id") is None, f"Lease should be cleared, got row={row}"

    def test_clear_lease_noop_when_no_existing_lease(self) -> None:
        """clear_lease does not raise when no lease exists for worker_id."""
        store = _make_store()

        # Should not raise even if no lease was set
        clear_lease(store, "nonexistent-worker")
