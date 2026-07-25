#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord

if TYPE_CHECKING:
    import aiosqlite

# The column list is spelled out in each statement rather than interpolated from
# a shared constant: an f-string here reads as a SQL-injection vector to both
# ruff (S608) and bandit (B608), and silencing two linters is worse than one
# repeated literal. Explicit columns (not SELECT *) also mean a future migration
# that adds a column cannot silently shift the row order underneath the reader.
_SELECT_BY_ID = (
    "SELECT target_id, tenant_id, display_name, protocol, endpoint, secret, width, height, "
    "is_system, is_static, ca_secret_ref, client_cert_secret_ref, client_key_secret_ref, "
    "config, created_by, created_at, updated_by, updated_at "
    "FROM cp_graphical_targets WHERE target_id = ?"
)

_SELECT_ALL = (
    "SELECT target_id, tenant_id, display_name, protocol, endpoint, secret, width, height, "
    "is_system, is_static, ca_secret_ref, client_cert_secret_ref, client_key_secret_ref, "
    "config, created_by, created_at, updated_by, updated_at "
    "FROM cp_graphical_targets ORDER BY target_id"
)


def _row_data(row: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", dict(row))


def _to_record(data: dict[str, Any]) -> GraphicalTargetRecord:
    """Rebuild a record from a row.

    ``config`` round-trips through JSON text.  A row whose config fails to
    decode is surfaced as an empty object rather than raising: the column is
    non-authoritative protocol metadata, and refusing to list every target
    because one row has a bad blob would turn a cosmetic defect into an
    outage.
    """
    raw_config = data.get("config") or "{}"
    try:
        decoded = json.loads(raw_config)
    except (TypeError, ValueError):
        decoded = {}
    if not isinstance(decoded, dict):
        decoded = {}
    return GraphicalTargetRecord(
        target_id=data["target_id"],
        tenant_id=data["tenant_id"],
        display_name=data["display_name"],
        protocol=data["protocol"],
        endpoint=data["endpoint"],
        secret=data["secret"],
        width=data["width"],
        height=data["height"],
        is_system=bool(data["is_system"]),
        is_static=bool(data["is_static"]),
        ca_secret_ref=data["ca_secret_ref"],
        client_cert_secret_ref=data["client_cert_secret_ref"],
        client_key_secret_ref=data["client_key_secret_ref"],
        config=decoded,
        created_by=data["created_by"],
        created_at=data["created_at"],
        updated_by=data["updated_by"],
        updated_at=data["updated_at"],
    )


class SqliteGraphicalTargetStore:
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

    async def put_graphical_target(self, record: GraphicalTargetRecord) -> None:
        await self._conn.execute(
            """
            INSERT INTO cp_graphical_targets(
                target_id, tenant_id, display_name, protocol, endpoint, secret,
                width, height, is_system, is_static, ca_secret_ref,
                client_cert_secret_ref, client_key_secret_ref, config,
                created_by, created_at, updated_by, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                display_name = excluded.display_name,
                protocol = excluded.protocol,
                endpoint = excluded.endpoint,
                secret = excluded.secret,
                width = excluded.width,
                height = excluded.height,
                is_system = excluded.is_system,
                is_static = excluded.is_static,
                ca_secret_ref = excluded.ca_secret_ref,
                client_cert_secret_ref = excluded.client_cert_secret_ref,
                client_key_secret_ref = excluded.client_key_secret_ref,
                config = excluded.config,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (
                record.target_id,
                record.tenant_id,
                record.display_name,
                record.protocol,
                record.endpoint,
                record.secret,
                record.width,
                record.height,
                int(record.is_system),
                int(record.is_static),
                record.ca_secret_ref,
                record.client_cert_secret_ref,
                record.client_key_secret_ref,
                json.dumps(record.config, sort_keys=True),
                record.created_by,
                record.created_at,
                record.updated_by,
                record.updated_at,
            ),
        )

    async def get_graphical_target(self, target_id: str) -> GraphicalTargetRecord | None:
        cursor = await self._conn.execute(_SELECT_BY_ID, (target_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _to_record(_row_data(row))

    async def list_graphical_targets(self) -> list[GraphicalTargetRecord]:
        cursor = await self._conn.execute(_SELECT_ALL)
        rows = await cursor.fetchall()
        await cursor.close()
        return [_to_record(_row_data(row)) for row in rows]

    async def delete_graphical_target(self, target_id: str) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM cp_graphical_targets WHERE target_id = ?",
            (target_id,),
        )
        removed = cursor.rowcount
        await cursor.close()
        return bool(removed)
