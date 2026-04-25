from __future__ import annotations

import aiosqlite
import time
from dataclasses import asdict

from provide.terminal.control.plane.lease import LeaseRecord


class SqliteLeaseStore:
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

    async def put_lease(self, record: LeaseRecord) -> None:
        data = asdict(record)
        await self._conn.execute(
            """
            INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                hijack_id = excluded.hijack_id,
                owner = excluded.owner,
                lease_expires_at = excluded.lease_expires_at,
                created_at = excluded.created_at,
                deleted_at = excluded.deleted_at
            """,
            (
                data["session_id"],
                data["hijack_id"],
                data["owner"],
                data["lease_expires_at"],
                data["created_at"],
                data["deleted_at"],
            ),
        )

    async def get_lease(self, session_id: str) -> LeaseRecord | None:
        cursor = await self._conn.execute("SELECT * FROM cp_leases WHERE session_id = ?", (session_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        data = dict(row)
        if data.get("deleted_at") is not None:
            return None
        return LeaseRecord(**data)

    async def clear_lease(self, session_id: str) -> None:
        await self._conn.execute(
            "UPDATE cp_leases SET deleted_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
