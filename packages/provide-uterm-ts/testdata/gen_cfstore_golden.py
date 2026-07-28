#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the Durable Object state store.

This is a Durable Object's own SQLite, which is where a session's state
survives the object being evicted and restarted. Everything a reconnecting
browser is told comes back through here.

**A lease is the right to type.** It carries an owner and an expiry, and
clearing it must clear all of it — a half-cleared lease leaves an owner with
no expiry, which reads as a lease that never ends.

**Deleting is marking, not removing.** A deleted session keeps its row with a
timestamp, so a request arriving afterwards is told the session is gone rather
than that it never existed. Undeleting is how a session id is reused.

**Metadata is upserted, never appended.** A Durable Object rewrites its own
row on every restart, and a second row for one session would make the fleet
list ambiguous.

**A migration runs on every start.** The tables are created if absent, and the
two later columns are added inside a suppressed failure — because they will
already exist on every start after the first, and a store that refused to open
would lose the session rather than the column.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfstore_golden.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.state.store import LeaseRecord, SqliteStateStore

OUT = Path(__file__).with_name("cfstore_golden.json")

# A fixed instant, so recorded timestamps do not move.
NOW = 1_760_000_000.0


def _store() -> tuple[SqliteStateStore, sqlite3.Connection]:
    """A migrated store over an in-memory database."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    def execute(sql: str, *params: object) -> Any:
        flat = params[0] if len(params) == 1 and isinstance(params[0], tuple) else params
        return connection.execute(sql, flat)

    store = SqliteStateStore(execute)
    store.migrate()
    return store, connection


def _tables(connection: sqlite3.Connection) -> list[str]:
    """Every table the migration created."""
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [row[0] for row in rows]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    """A table's columns, in order."""
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    """How many rows a table holds."""
    # The table name is interpolated because SQLite cannot parameterise an
    # identifier. It is one of this module's own constants, never caller input.
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


# (name, meta) — what a session says about itself.
META_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "everything supplied",
        {
            "display_name": "Session One",
            "connector_type": "ssh",
            "created_at": 1234.5,
            "tags": ["a", "b"],
            "visibility": "private",
            "owner": "alice",
        },
    ),
    ("nothing supplied", {}),
    ("empty strings", {"display_name": "", "connector_type": "", "visibility": "", "tags": []}),
    ("no owner", {"display_name": "x", "owner": None}),
    ("a created time of zero", {"created_at": 0}),
    ("unicode in the name", {"display_name": "héllo → ✓"}),
]


def _record_meta() -> list[dict[str, Any]]:
    """What a metadata round trip preserves."""
    records = []
    for name, meta in META_CASES:
        store, connection = _store()
        store.save_session_meta("w1", meta)
        loaded = store.load_session_meta("w1")
        # created_at defaults to "now" when absent, which cannot be recorded.
        stamped = loaded is not None and "created_at" in meta and meta["created_at"] != 0
        records.append(
            {
                "name": name,
                "meta": meta,
                "loaded": {k: v for k, v in (loaded or {}).items() if k != "created_at"} if loaded else None,
                "created_at": loaded["created_at"] if loaded and stamped else None,
                "rows": _row_count(connection, "session_meta"),
            }
        )
    return records


def _record_meta_upsert() -> dict[str, Any]:
    """A second save replaces the first rather than adding to it."""
    store, connection = _store()
    store.save_session_meta("w1", {"display_name": "first", "created_at": 1.0})
    store.save_session_meta("w1", {"display_name": "second", "created_at": 2.0})
    store.save_session_meta("w2", {"display_name": "other", "created_at": 3.0})
    return {
        "loaded": store.load_session_meta("w1"),
        "rows": _row_count(connection, "session_meta"),
        "missing": store.load_session_meta("nobody"),
    }


def _record_invite_state() -> dict[str, Any]:
    """Consumed one-time invite digests."""
    store, connection = _store()
    missing = store.load_tunnel_invite_state("w1")
    store.save_tunnel_invite_state("w1", {"share": "sha256:abc", "consumed_at": 12.0})
    first = store.load_tunnel_invite_state("w1")
    store.save_tunnel_invite_state("w1", {"share": "sha256:def"})
    second = store.load_tunnel_invite_state("w1")

    corrupt, _ = _store()
    corrupt._run(
        "INSERT INTO tunnel_invite_state(worker_id,entry_json,updated_at) VALUES(?,?,?)", "w1", "{not json", 0.0
    )
    corrupt_result = corrupt.load_tunnel_invite_state("w1")

    not_object, _ = _store()
    not_object._run(
        "INSERT INTO tunnel_invite_state(worker_id,entry_json,updated_at) VALUES(?,?,?)", "w1", '["a"]', 0.0
    )
    not_object_result = not_object.load_tunnel_invite_state("w1")

    return {
        "missing": missing,
        "first": first,
        "after_second_save": second,
        "rows": _row_count(connection, "tunnel_invite_state"),
        "corrupt": corrupt_result,
        "not_an_object": not_object_result,
    }


def _record_session_state() -> dict[str, Any]:
    """The lease, the snapshot, the input mode and the tombstone."""
    store, connection = _store()
    missing = store.load_session("w1")

    store.save_lease(LeaseRecord(worker_id="w1", hijack_id="h1", owner="alice", lease_expires_at=NOW + 60))
    after_lease = store.load_session("w1")

    store.save_snapshot("w1", {"screen": "hello", "cols": 80})
    after_snapshot = store.load_session("w1")

    store.save_input_mode("w1", "observe")
    after_mode = store.load_session("w1")

    store.clear_lease("w1")
    after_clear = store.load_session("w1")

    store.mark_deleted("w1")
    after_delete = store.load_session("w1")

    # A lease saved onto a fresh worker id, so the row is created rather than
    # updated — the insert and the update paths differ.
    store.save_lease(LeaseRecord(worker_id="w2", hijack_id="h2", owner="bob", lease_expires_at=NOW + 30))
    fresh = store.load_session("w2")

    # Saving a mode for a session that has no row yet.
    store.save_input_mode("w3", "observe")
    mode_only = store.load_session("w3")

    # And a snapshot for one that has no row yet.
    store.save_snapshot("w4", {"screen": "x"})
    snapshot_only = store.load_session("w4")

    return {
        "missing": missing,
        "after_lease": after_lease,
        "after_snapshot": after_snapshot,
        "after_mode": after_mode,
        "after_clear": _without_deleted(after_clear),
        "after_delete_has_timestamp": after_delete is not None and after_delete["deleted_at"] is not None,
        "after_delete_keeps_row": _row_count(connection, "session_state") > 0,
        "fresh_worker": fresh,
        "mode_only": mode_only,
        "snapshot_only": snapshot_only,
    }


def _record_events() -> dict[str, Any]:
    """The event log: numbering, trimming, and catching up.

    Sequence numbers are per session and never reused, because a browser asks
    "what have I missed since N" — a number that went backwards would replay
    events it had already seen, or skip ones it had not.

    Trimming keeps the newest. A session that ran for hours would otherwise
    grow without bound in a Durable Object's storage, and it is the recent
    events a reconnecting browser needs.
    """
    store, _ = _store()
    empty_seq = store.current_event_seq("w1")
    empty_min = store.min_event_seq("w1")
    empty_count = store.count_events("w1")
    empty_list = store.list_events_since("w1", 0)

    first = store.append_event("w1", "output", {"text": "hello"})
    second = store.append_event("w1", "output", {"text": "world"})
    third = store.append_event("w1", "resize", {"cols": 80, "rows": 24})

    # A second session numbers from one again: the sequence is per session.
    other = store.append_event("w2", "output", {"text": "other"})

    listed = store.list_events_since("w1", 0)
    since_first = store.list_events_since("w1", 1)
    since_last = store.list_events_since("w1", 3)
    limited = store.list_events_since("w1", 0, limit=2)
    beyond = store.list_events_since("w1", 99)

    # Trimming, with room for three.
    trimmed_store, trimmed_conn = _store()
    trimmed_store._max_events = 3
    for index in range(6):
        trimmed_store.append_event("t1", "output", {"n": index})
    trimmed_kept = trimmed_store.list_events_since("t1", 0)
    trimmed_min = trimmed_store.min_event_seq("t1")
    trimmed_seq = trimmed_store.current_event_seq("t1")

    # One session's trimming must not touch another's.
    trimmed_store.append_event("t2", "output", {"n": 0})
    for index in range(6):
        trimmed_store.append_event("t1", "output", {"n": index})
    other_survives = trimmed_store.count_events("t2")

    return {
        "empty_seq": empty_seq,
        "empty_min": empty_min,
        "empty_count": empty_count,
        "empty_list": empty_list,
        # The timestamps are wall clocks; only their shape is recorded.
        "first": {k: v for k, v in first.items() if k != "ts"},
        "second": {k: v for k, v in second.items() if k != "ts"},
        "third": {k: v for k, v in third.items() if k != "ts"},
        "other_session_first": {k: v for k, v in other.items() if k != "ts"},
        "listed": [{k: v for k, v in event.items() if k != "ts"} for event in listed],
        "since_first": [event["seq"] for event in since_first],
        "since_last": [event["seq"] for event in since_last],
        "limited": [event["seq"] for event in limited],
        "beyond": beyond,
        "count": store.count_events("w1"),
        "min": store.min_event_seq("w1"),
        "seq": store.current_event_seq("w1"),
        "session_row_seq": (store.load_session("w1") or {}).get("event_seq"),
        "trimmed_kept": [event["seq"] for event in trimmed_kept],
        "trimmed_min": trimmed_min,
        "trimmed_seq": trimmed_seq,
        "trimmed_count": trimmed_store.count_events("t1"),
        "other_survives": other_survives,
        "rows_after_trim": _row_count(trimmed_conn, "session_events"),
    }


def _without_deleted(session: dict[str, Any] | None) -> dict[str, Any] | None:
    """A session without its deletion timestamp, which is a wall clock."""
    if session is None:
        return None
    return {k: v for k, v in session.items() if k != "deleted_at"}


def main() -> int:
    """Write the golden corpus and report what it covers."""
    store, connection = _store()
    # Migrating twice must be safe: it runs on every Durable Object start.
    store.migrate()

    corpus = {
        "tables": _tables(connection),
        "columns": {table: _columns(connection, table) for table in _tables(connection)},
        "meta": _record_meta(),
        "meta_upsert": _record_meta_upsert(),
        "invite_state": _record_invite_state(),
        "session_state": _record_session_state(),
        "events": _record_events(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['tables'])} tables, {len(META_CASES)} meta cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
