#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.control.plane.approval import ApprovalRecord

if TYPE_CHECKING:
    import aiosqlite


def _row_data(row: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", dict(row))


class SqliteApprovalStore:
    def __init__(self, tx: Any) -> None:
        self._tx = tx

    @property
    def _conn(self) -> aiosqlite.Connection:
        conn = getattr(self._tx, "conn", None)
        if conn is None:
            conn = getattr(self._tx, "_conn", None)
        if conn is None:  # pragma: no cover - defensive guard for bad adapters
            raise AttributeError("transaction has no sqlite connection")
        return cast("aiosqlite.Connection", conn)

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
        return ApprovalRecord(**_row_data(row))

    async def list_pending(self) -> list[ApprovalRecord]:
        cursor = await self._conn.execute(
            "SELECT * FROM cp_approvals WHERE state = 'pending' ORDER BY created_at ASC, approval_id ASC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [ApprovalRecord(**_row_data(row)) for row in rows]
