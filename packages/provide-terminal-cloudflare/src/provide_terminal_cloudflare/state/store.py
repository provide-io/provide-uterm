from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol


class SqlExecutor(Protocol):
    def __call__(self, sql: str, *params: object) -> Any: ...


@dataclass(slots=True)
class LeaseRecord:
    worker_id: str
    hijack_id: str
    owner: str
    lease_expires_at: float


class SqliteStateStore:
    """Durable Object SQLite-backed store for session state."""

    def __init__(self, exec_sql: SqlExecutor, max_events_per_worker: int = 2000):
        self._exec = exec_sql
        self._max_events = max(1, max_events_per_worker)

    def _run(self, sql: str, *params: object) -> Any:
        if not params:
            return self._exec(sql)
        try:
            # CF Workers sql.exec API: exec(sql, *params) — variadic positional args.
            return self._exec(sql, *params)
        except Exception as first_exc:
            # Fallback for DB-API executors (e.g. sqlite3 in tests) that expect a
            # params tuple rather than variadic args.  If the fallback also fails,
            # re-raise the *original* error so that real SQL errors are not masked.
            try:
                return self._exec(sql, params)
            except Exception:
                raise first_exc from None

    def migrate(self) -> None:
        self._run(
            """
            CREATE TABLE IF NOT EXISTS session_state (
                worker_id TEXT PRIMARY KEY,
                hijack_id TEXT,
                owner TEXT,
                lease_expires_at REAL,
                last_snapshot_json TEXT,
                event_seq INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        self._run(
            """
            CREATE TABLE IF NOT EXISTS session_events (
                worker_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (worker_id, seq)
            )
            """
        )
        # Add input_mode column if it does not exist yet (idempotent migration).
        with contextlib.suppress(Exception):
            self._run("ALTER TABLE session_state ADD COLUMN input_mode TEXT NOT NULL DEFAULT 'hijack'")

    def load_session(self, worker_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._run(
                """
                SELECT worker_id, hijack_id, owner, lease_expires_at, last_snapshot_json, event_seq, input_mode
                FROM session_state
                WHERE worker_id = ?
                """,
                worker_id,
            )
        )
        if not rows:
            return None
        row = rows[0]
        snapshot_raw = self._row_value(row, "last_snapshot_json", 4)
        return {
            "worker_id": self._row_value(row, "worker_id", 0),
            "hijack_id": self._row_value(row, "hijack_id", 1),
            "owner": self._row_value(row, "owner", 2),
            "lease_expires_at": self._row_value(row, "lease_expires_at", 3),
            "last_snapshot": json.loads(snapshot_raw) if snapshot_raw else None,
            "event_seq": int(self._row_value(row, "event_seq", 5) or 0),
            "input_mode": str(self._row_value(row, "input_mode", 6) or "hijack"),
        }

    def save_lease(self, record: LeaseRecord) -> None:
        now = time.time()
        self._run(
            """
            INSERT INTO session_state(worker_id, hijack_id, owner, lease_expires_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                hijack_id = excluded.hijack_id,
                owner = excluded.owner,
                lease_expires_at = excluded.lease_expires_at,
                updated_at = excluded.updated_at
            """,
            record.worker_id,
            record.hijack_id,
            record.owner,
            float(record.lease_expires_at),
            now,
        )

    def clear_lease(self, worker_id: str) -> None:
        self._run(
            """
            UPDATE session_state
            SET hijack_id = NULL, owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE worker_id = ?
            """,
            time.time(),
            worker_id,
        )

    def save_input_mode(self, worker_id: str, mode: str) -> None:
        now = time.time()
        self._run(
            """
            INSERT INTO session_state(worker_id, input_mode, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                input_mode = excluded.input_mode,
                updated_at = excluded.updated_at
            """,
            worker_id,
            mode,
            now,
        )

    def min_event_seq(self, worker_id: str) -> int:
        rows = self._rows(
            self._run(
                """
                SELECT COALESCE(MIN(seq), 0) AS seq
                FROM session_events
                WHERE worker_id = ?
                """,
                worker_id,
            )
        )
        if not rows:
            return 0
        row = rows[0]
        if isinstance(row, dict):
            return int(row.get("seq") or 0)
        if hasattr(row, "keys") and hasattr(row, "__getitem__"):
            return int(row["seq"] if "seq" in row else self._get(row, 0) or 0)
        return int(self._get(row, 0) or 0)

    def save_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=True)
        now = time.time()
        self._run(
            """
            INSERT INTO session_state(worker_id, last_snapshot_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                last_snapshot_json = excluded.last_snapshot_json,
                updated_at = excluded.updated_at
            """,
            worker_id,
            payload,
            now,
        )

    def append_event(self, worker_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Wrapped in SAVEPOINT for crash atomicity — prevents orphan events
        # if the DO crashes between INSERT and UPDATE/DELETE.  SAVEPOINT works
        # inside existing transactions (Python sqlite3 auto-begins), unlike
        # bare BEGIN which would fail.
        current_seq = self.current_event_seq(worker_id)
        seq = current_seq + 1
        ts = time.time()
        serialized_payload = json.dumps(payload, ensure_ascii=True)
        self._run("SAVEPOINT append_event")
        try:
            self._run(
                """
                INSERT INTO session_events(worker_id, seq, ts, event_type, payload_json)
                VALUES(?, ?, ?, ?, ?)
                """,
                worker_id,
                seq,
                ts,
                event_type,
                serialized_payload,
            )
            self._run(
                """
                INSERT INTO session_state(worker_id, event_seq, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    event_seq = excluded.event_seq,
                    updated_at = excluded.updated_at
                """,
                worker_id,
                seq,
                ts,
            )
            # Prune oldest rows so the table never exceeds max_events_per_worker.
            self._run(
                """
                DELETE FROM session_events
                WHERE worker_id = ? AND seq <= ? - ?
                """,
                worker_id,
                seq,
                self._max_events,
            )
            self._run("RELEASE SAVEPOINT append_event")
        except Exception:
            with contextlib.suppress(Exception):
                self._run("ROLLBACK TO SAVEPOINT append_event")
            raise
        return {"seq": seq, "ts": ts, "type": event_type, "data": payload}

    def current_event_seq(self, worker_id: str) -> int:
        rows = self._rows(
            self._run(
                """
                SELECT COALESCE(MAX(seq), 0) AS seq
                FROM session_events
                WHERE worker_id = ?
                """,
                worker_id,
            )
        )
        if not rows:
            return 0
        row = rows[0]
        if isinstance(row, dict):
            return int(row.get("seq") or 0)
        if hasattr(row, "keys") and hasattr(row, "__getitem__"):
            return int(row["seq"] if "seq" in row else self._get(row, 0) or 0)
        return int(self._get(row, 0) or 0)

    def list_events_since(self, worker_id: str, seq: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._rows(
            self._run(
                """
                SELECT seq, ts, event_type, payload_json
                FROM session_events
                WHERE worker_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                worker_id,
                int(seq),
                int(limit),
            )
        )
        return [
            {
                "seq": int(self._row_value(row, "seq", 0) or 0),
                "ts": float(self._row_value(row, "ts", 1) or 0.0),
                "type": str(self._row_value(row, "event_type", 2) or ""),
                "data": json.loads(str(self._row_value(row, "payload_json", 3) or "{}")),
            }
            for row in rows
        ]

    @classmethod
    def _row_value(cls, row: Any, key: str, idx: int) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        if hasattr(row, "get"):
            with contextlib.suppress(Exception):
                value = row.get(key)
                if value is not None:
                    return value
        if hasattr(row, key):
            with contextlib.suppress(Exception):
                return getattr(row, key)
        if hasattr(row, "to_py"):
            try:
                py_row = row.to_py()
            except Exception:
                py_row = None
            if py_row is not None:
                return cls._row_value(py_row, key, idx)
        return cls._get(row, idx)

    @staticmethod
    def _rows(result: Any) -> list[Any]:
        if result is None:
            return []
        to_array = getattr(result, "toArray", None)
        if callable(to_array):
            return list(to_array())
        if isinstance(result, list):
            return result
        if hasattr(result, "fetchall"):
            return list(result.fetchall())
        return []

    @staticmethod
    def _get(row: Any, idx: int) -> Any:
        if isinstance(row, dict):
            values = list(row.values())
            return values[idx] if idx < len(values) else None
        if hasattr(row, "keys") and hasattr(row, "__getitem__"):
            keys = list(row.keys())
            if idx >= len(keys):
                return None
            return row[keys[idx]]
        if hasattr(row, "to_py"):
            try:
                py_row = row.to_py()
            except Exception:
                py_row = None
            if py_row is not None:
                return SqliteStateStore._get(py_row, idx)
        return row[idx]
