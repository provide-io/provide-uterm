from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from . import _row_utils


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
        for ddl in (
            """CREATE TABLE IF NOT EXISTS session_state (
                worker_id TEXT PRIMARY KEY, hijack_id TEXT, owner TEXT,
                lease_expires_at REAL, last_snapshot_json TEXT, deleted_at REAL,
                event_seq INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS session_events (
                worker_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (worker_id, seq))""",
            """CREATE TABLE IF NOT EXISTS resume_tokens (
                token TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer', was_hijack_owner INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, expires_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS webhooks (
                webhook_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, url TEXT NOT NULL,
                event_types_json TEXT, pattern TEXT, secret TEXT)""",
            """CREATE TABLE IF NOT EXISTS session_meta (
                worker_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                connector_type TEXT NOT NULL DEFAULT 'unknown', created_at REAL NOT NULL DEFAULT 0,
                tags_json TEXT NOT NULL DEFAULT '[]', visibility TEXT NOT NULL DEFAULT 'public',
                owner TEXT)""",
        ):
            self._run(ddl)
        with contextlib.suppress(Exception):
            self._run("ALTER TABLE session_state ADD COLUMN input_mode TEXT NOT NULL DEFAULT 'hijack'")
        with contextlib.suppress(Exception):
            self._run("ALTER TABLE session_state ADD COLUMN deleted_at REAL")

    # ------------------------------------------------------------------
    # Session metadata (display_name, connector_type, created_at, etc.)
    # ------------------------------------------------------------------

    def save_session_meta(self, worker_id: str, meta: dict[str, Any]) -> None:
        """Persist session metadata to SQLite (UPSERT)."""
        self._run(
            "INSERT INTO session_meta(worker_id,display_name,connector_type,created_at,tags_json,visibility,owner) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET display_name=excluded.display_name,"
            "connector_type=excluded.connector_type,created_at=excluded.created_at,"
            "tags_json=excluded.tags_json,visibility=excluded.visibility,owner=excluded.owner",
            worker_id,
            str(meta.get("display_name") or worker_id),
            str(meta.get("connector_type") or "unknown"),
            float(meta.get("created_at") or time.time()),
            json.dumps(meta.get("tags") or [], ensure_ascii=True),
            str(meta.get("visibility") or "public"),
            meta.get("owner"),
        )

    def load_session_meta(self, worker_id: str) -> dict[str, Any] | None:
        """Load persisted session metadata, or ``None`` if never saved."""
        rows = self._rows(
            self._run(
                "SELECT display_name,connector_type,created_at,tags_json,visibility,owner "
                "FROM session_meta WHERE worker_id=?",
                worker_id,
            )
        )
        if not rows:
            return None
        r, rv = rows[0], self._row_value
        return {
            "display_name": str(rv(r, "display_name", 0) or worker_id),
            "connector_type": str(rv(r, "connector_type", 1) or "unknown"),
            "created_at": float(rv(r, "created_at", 2) or 0),
            "tags": json.loads(str(rv(r, "tags_json", 3) or "[]")),
            "visibility": str(rv(r, "visibility", 4) or "public"),
            "owner": rv(r, "owner", 5),
        }

    # ---- Session state (hijack lease, snapshot, events) ----

    def load_session(self, worker_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._run(
                """
                SELECT worker_id, hijack_id, owner, lease_expires_at, last_snapshot_json, event_seq, input_mode
                     , deleted_at
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
            "deleted_at": self._row_value(row, "deleted_at", 7),
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

    def mark_deleted(self, worker_id: str) -> None:
        """Persist a tombstone for a deleted session."""
        now = time.time()
        self._run(
            """
            INSERT INTO session_state(worker_id, deleted_at, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                hijack_id = NULL,
                owner = NULL,
                lease_expires_at = NULL,
                last_snapshot_json = NULL,
                deleted_at = excluded.deleted_at,
                updated_at = excluded.updated_at
            """,
            worker_id,
            now,
            now,
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
            self._run("SELECT COALESCE(MIN(seq), 0) AS seq FROM session_events WHERE worker_id = ?", worker_id)
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
        # CF DO SQLite auto-coalesces writes atomically (no SAVEPOINT/ROLLBACK).
        current_seq = self.current_event_seq(worker_id)
        seq = current_seq + 1
        ts = time.time()
        serialized_payload = json.dumps(payload, ensure_ascii=True)
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
        return {"seq": seq, "ts": ts, "type": event_type, "data": payload}

    def current_event_seq(self, worker_id: str) -> int:
        rows = self._rows(
            self._run("SELECT COALESCE(MAX(seq), 0) AS seq FROM session_events WHERE worker_id = ?", worker_id)
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

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def count_events(self, worker_id: str) -> int:
        """Return the total number of events stored for *worker_id*."""
        rows = self._rows(self._run("SELECT COUNT(*) AS cnt FROM session_events WHERE worker_id = ?", worker_id))
        # COUNT(*) always returns exactly one row.
        return int(self._row_value(rows[0], "cnt", 0) or 0)

    def list_recording_entries(
        self,
        worker_id: str,
        *,
        limit: int = 200,
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query session events as ``{ts, event, data}``.  *offset=None* → tail."""
        limit = max(1, min(limit, 500))
        tail = offset is None

        where = "WHERE worker_id = ?"
        params: list[object] = [worker_id]
        if event is not None:
            where += " AND event_type = ?"
            params.append(event)

        order = "ORDER BY seq DESC" if tail else "ORDER BY seq ASC"
        params.append(limit)
        suffix = f"{order} LIMIT ?"
        if not tail:
            suffix += " OFFSET ?"
            # ``tail`` is True iff ``offset is None``; narrow for mypy here.
            assert offset is not None
            params.append(max(0, offset))

        # nosec B608  # `where` is built from string-literal fragments above
        # (no user data) and every user value (session_id, event, limit,
        # offset) goes through `?` placeholders in `params`. The f-string
        # only stitches static SQL together.
        sql = f"SELECT ts, event_type, payload_json FROM session_events {where} {suffix}"  # noqa: S608  # nosec B608
        rows = self._rows(self._run(sql, *params))
        if tail:
            rows = list(reversed(rows))

        return [
            {
                "ts": float(self._row_value(row, "ts", 0) or 0.0),
                "event": str(self._row_value(row, "event_type", 1) or ""),
                "data": json.loads(str(self._row_value(row, "payload_json", 2) or "{}")),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def save_webhook(
        self,
        webhook_id: str,
        session_id: str,
        url: str,
        *,
        event_types: list[str] | None = None,
        pattern: str | None = None,
        secret: str | None = None,
    ) -> None:
        event_types_json = json.dumps(event_types) if event_types is not None else None
        self._run(
            """
            INSERT INTO webhooks(webhook_id, session_id, url, event_types_json, pattern, secret)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(webhook_id) DO UPDATE SET
                url = excluded.url,
                event_types_json = excluded.event_types_json,
                pattern = excluded.pattern,
                secret = excluded.secret
            """,
            webhook_id,
            session_id,
            url,
            event_types_json,
            pattern,
            secret,
        )

    def load_webhooks(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._rows(
            self._run(
                """
                SELECT webhook_id, session_id, url, event_types_json, pattern, secret
                FROM webhooks
                WHERE session_id = ?
                """,
                session_id,
            )
        )
        return [
            {
                "webhook_id": str(self._row_value(row, "webhook_id", 0) or ""),
                "session_id": str(self._row_value(row, "session_id", 1) or ""),
                "url": str(self._row_value(row, "url", 2) or ""),
                "event_types": json.loads(str(self._row_value(row, "event_types_json", 3) or "null")),
                "pattern": self._row_value(row, "pattern", 4),
                "secret": self._row_value(row, "secret", 5),
            }
            for row in rows
        ]

    def delete_webhook(self, webhook_id: str) -> bool:
        rows_before = self._rows(self._run("SELECT webhook_id FROM webhooks WHERE webhook_id = ?", webhook_id))
        if not rows_before:
            return False
        self._run("DELETE FROM webhooks WHERE webhook_id = ?", webhook_id)
        return True

    # ------------------------------------------------------------------
    # Resume tokens
    # ------------------------------------------------------------------

    def create_resume_token(self, token: str, worker_id: str, role: str, ttl_s: float) -> None:
        now = time.time()
        self._run(
            """
            INSERT INTO resume_tokens(token, worker_id, role, was_hijack_owner, created_at, expires_at)
            VALUES(?, ?, ?, 0, ?, ?)
            """,
            token,
            worker_id,
            role,
            now,
            now + ttl_s,
        )

    def get_resume_token(self, token: str) -> dict[str, Any] | None:
        rows = self._rows(
            self._run(
                "SELECT token, worker_id, role, was_hijack_owner, created_at, expires_at FROM resume_tokens WHERE token = ?",
                token,
            )
        )
        if not rows:
            return None
        row = rows[0]
        expires_at = float(self._row_value(row, "expires_at", 5) or 0)
        if time.time() > expires_at:
            self.revoke_resume_token(token)
            return None
        return {
            "token": self._row_value(row, "token", 0),
            "worker_id": self._row_value(row, "worker_id", 1),
            "role": str(self._row_value(row, "role", 2) or "viewer"),
            "was_hijack_owner": bool(int(self._row_value(row, "was_hijack_owner", 3) or 0)),
            "created_at": float(self._row_value(row, "created_at", 4) or 0),
            "expires_at": expires_at,
        }

    def mark_resume_hijack_owner(self, token: str, is_owner: bool) -> None:
        self._run(
            "UPDATE resume_tokens SET was_hijack_owner = ? WHERE token = ?",
            1 if is_owner else 0,
            token,
        )

    def revoke_resume_token(self, token: str) -> None:
        self._run("DELETE FROM resume_tokens WHERE token = ?", token)

    def cleanup_expired_tokens(self) -> int:
        now = time.time()
        self._run("DELETE FROM resume_tokens WHERE expires_at <= ?", now)
        return 0  # row count not available through all executors

    # Row-shape adapters live in ``_row_utils`` so this module stays focused
    # on SQL/state behaviour. Exposed as staticmethods on the class for
    # backwards compatibility with callers using ``SqliteStateStore._rows`` etc.
    _row_value = staticmethod(_row_utils.row_value)
    _rows = staticmethod(_row_utils.rows)
    _get = staticmethod(_row_utils.get_by_index)
