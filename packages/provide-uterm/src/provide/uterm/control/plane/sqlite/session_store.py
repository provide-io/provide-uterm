from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from provide.uterm.control.plane.session import SessionRecord

if TYPE_CHECKING:
    import aiosqlite


class SqliteSessionStore:
    def __init__(self, tx) -> None:
        self._tx = tx

    @property
    def _conn(self) -> aiosqlite.Connection:
        conn = getattr(self._tx, "conn", None)
        if conn is None:
            conn = getattr(self._tx, "_conn", None)
        if conn is None:  # pragma: no cover - defensive guard for bad adapters
            raise AttributeError("transaction has no sqlite connection")
        return conn

    async def upsert_session(self, record: SessionRecord) -> None:
        data = asdict(record)
        await self._conn.execute(
            """
            INSERT INTO cp_sessions(
                session_id, display_name, connector_type, owner, visibility,
                lifecycle_state, created_at, updated_at, deleted_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                display_name = excluded.display_name,
                connector_type = excluded.connector_type,
                owner = excluded.owner,
                visibility = excluded.visibility,
                lifecycle_state = excluded.lifecycle_state,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                deleted_at = excluded.deleted_at
            """,
            (
                data["session_id"],
                data["display_name"],
                data["connector_type"],
                data["owner"],
                data["visibility"],
                data["lifecycle_state"],
                data["created_at"],
                data["updated_at"],
                data["deleted_at"],
            ),
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        cursor = await self._conn.execute("SELECT * FROM cp_sessions WHERE session_id = ?", (session_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return SessionRecord(**dict(row))

    async def mark_deleted(self, session_id: str, deleted_at: float) -> None:
        await self._conn.execute(
            """
            UPDATE cp_sessions
            SET lifecycle_state = 'deleted',
                deleted_at = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (deleted_at, deleted_at, session_id),
        )
