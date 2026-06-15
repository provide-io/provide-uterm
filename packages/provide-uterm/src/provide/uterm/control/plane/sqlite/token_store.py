#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.control.plane.token import ResumeTokenRecord, SessionTokenRecord

if TYPE_CHECKING:
    import aiosqlite


def _row_data(row: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", dict(row))


class SqliteTokenStore:
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

    async def put_session_token(self, record: SessionTokenRecord) -> None:
        data = asdict(record)
        await self._conn.execute(
            """
            INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at, expires_at, revoked_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, token_kind) DO UPDATE SET
                token_value = excluded.token_value,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                revoked_at = excluded.revoked_at
            """,
            (
                data["session_id"],
                data["token_kind"],
                data["token_value"],
                data["created_at"],
                data["expires_at"],
                data["revoked_at"],
            ),
        )

    async def get_session_token(self, session_id: str, token_kind: str) -> SessionTokenRecord | None:
        cursor = await self._conn.execute(
            "SELECT * FROM cp_session_tokens WHERE session_id = ? AND token_kind = ?",
            (session_id, token_kind),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return SessionTokenRecord(**_row_data(row))

    async def create_resume_token(self, record: ResumeTokenRecord) -> None:
        data = asdict(record)
        await self._conn.execute(
            """
            INSERT INTO cp_resume_tokens(
                token_value, session_id, role, created_at, expires_at, was_hijack_owner, revoked_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_value) DO UPDATE SET
                session_id = excluded.session_id,
                role = excluded.role,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                was_hijack_owner = excluded.was_hijack_owner,
                revoked_at = excluded.revoked_at
            """,
            (
                data["token_value"],
                data["session_id"],
                data["role"],
                data["created_at"],
                data["expires_at"],
                1 if data["was_hijack_owner"] else 0,
                data["revoked_at"],
            ),
        )

    async def get_resume_token(self, token_value: str) -> ResumeTokenRecord | None:
        cursor = await self._conn.execute("SELECT * FROM cp_resume_tokens WHERE token_value = ?", (token_value,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        data = _row_data(row)
        if data.get("revoked_at") is not None:
            return None
        return ResumeTokenRecord(
            token_value=str(data["token_value"]),
            session_id=str(data["session_id"]),
            role=str(data["role"]),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            was_hijack_owner=bool(data["was_hijack_owner"]),
            revoked_at=data["revoked_at"],
        )

    async def revoke_resume_token(self, token_value: str, revoked_at: float) -> None:
        await self._conn.execute(
            "UPDATE cp_resume_tokens SET revoked_at = ? WHERE token_value = ?",
            (revoked_at, token_value),
        )

    async def consume_resume_token(self, token_value: str, revoked_at: float) -> ResumeTokenRecord | None:
        cursor = await self._conn.execute("SELECT * FROM cp_resume_tokens WHERE token_value = ?", (token_value,))
        row = await cursor.fetchone()
        if row is None:
            await cursor.close()
            return None
        data = _row_data(row)
        if data.get("revoked_at") is not None:
            await cursor.close()
            return None
        await cursor.close()
        update_cursor = await self._conn.execute(
            "UPDATE cp_resume_tokens SET revoked_at = ? WHERE token_value = ? AND revoked_at IS NULL",
            (revoked_at, token_value),
        )
        if update_cursor.rowcount != 1:
            return None
        return ResumeTokenRecord(
            token_value=str(data["token_value"]),
            session_id=str(data["session_id"]),
            role=str(data["role"]),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            was_hijack_owner=bool(data["was_hijack_owner"]),
            revoked_at=None,
        )
