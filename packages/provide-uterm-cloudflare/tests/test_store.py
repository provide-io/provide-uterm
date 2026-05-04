from __future__ import annotations

import sqlite3

import pytest
from provide.terminal.cloudflare.state.store import LeaseRecord, SqliteStateStore


def test_store_migrate_idempotent_and_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)

    store.migrate()
    store.migrate()

    store.save_lease(LeaseRecord(worker_id="w1", hijack_id="h1", owner="alice", lease_expires_at=123.0))
    store.save_snapshot("w1", {"type": "snapshot", "screen": "abc"})
    event = store.append_event("w1", "snapshot", {"screen": "abc"})

    row = store.load_session("w1")
    assert row is not None
    assert row["hijack_id"] == "h1"
    assert row["owner"] == "alice"
    assert row["event_seq"] == event["seq"]
    assert row["last_snapshot"] == {"type": "snapshot", "screen": "abc"}

    events = store.list_events_since("w1", 0)
    assert len(events) == 1
    assert events[0]["type"] == "snapshot"


def test_store_run_propagates_sql_errors() -> None:
    """SQL errors must surface from _run(), not be silently swallowed.

    Regression for Bug C: the original _run() used a bare ``except Exception``
    that caught real SQL errors and retried with a different calling convention,
    making the original error invisible.  Now if both calling conventions fail,
    the *original* exception is re-raised so callers see the real problem.
    """
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    with pytest.raises((sqlite3.OperationalError, sqlite3.ProgrammingError)):
        # Querying a nonexistent table must raise, not silently return None.
        store._run("SELECT * FROM nonexistent_table_xyz WHERE id = ?", "val")
