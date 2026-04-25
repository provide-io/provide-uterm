from __future__ import annotations

import aiosqlite
from dataclasses import asdict

from provide.terminal.control.plane.approval import ApprovalRecord


class SqliteApprovalStore:
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

    async def put_approval(self, record: ApprovalRecord) -> None:
        data = asdict(record)
        await self._conn.execute(
            """
            INSERT INTO cp_approvals(
                approval_id, session_id, command, requested_by, state,
                created_at, resolved_at, resolved_by
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id) DO UPDATE SET
                session_id = excluded.session_id,
                command = excluded.command,
                requested_by = excluded.requested_by,
                state = excluded.state,
                created_at = excluded.created_at,
                resolved_at = excluded.resolved_at,
                resolved_by = excluded.resolved_by
            """,
            (
                data["approval_id"],
                data["session_id"],
                data["command"],
                data["requested_by"],
                data["state"],
                data["created_at"],
                data["resolved_at"],
                data["resolved_by"],
            ),
        )

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        cursor = await self._conn.execute("SELECT * FROM cp_approvals WHERE approval_id = ?", (approval_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return ApprovalRecord(**dict(row))

    async def list_pending(self) -> list[ApprovalRecord]:
        cursor = await self._conn.execute(
            "SELECT * FROM cp_approvals WHERE state = 'pending' ORDER BY created_at ASC, approval_id ASC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [ApprovalRecord(**dict(row)) for row in rows]
