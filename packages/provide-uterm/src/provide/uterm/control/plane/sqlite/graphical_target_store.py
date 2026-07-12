#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord

if TYPE_CHECKING:
    import aiosqlite


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


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
        data = asdict(record)
        await self._conn.execute(
            """
            INSERT INTO cp_graphical_targets VALUES(
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(target_id) DO UPDATE SET
                endpoint=excluded.endpoint, tls_mode=excluded.tls_mode,
                ca_secret_ref=excluded.ca_secret_ref, client_cert_secret_ref=excluded.client_cert_secret_ref,
                client_key_secret_ref=excluded.client_key_secret_ref,
                expected_server_name=excluded.expected_server_name, allowed_vm_patterns=excluded.allowed_vm_patterns,
                tenant_id=excluded.tenant_id, minimum_role=excluded.minimum_role,
                connect_timeout_s=excluded.connect_timeout_s, handshake_timeout_s=excluded.handshake_timeout_s,
                read_timeout_s=excluded.read_timeout_s, write_timeout_s=excluded.write_timeout_s,
                shutdown_timeout_s=excluded.shutdown_timeout_s,
                max_grpc_message_bytes=excluded.max_grpc_message_bytes,
                max_framebuffer_width=excluded.max_framebuffer_width,
                max_framebuffer_height=excluded.max_framebuffer_height,
                max_rectangles=excluded.max_rectangles, max_clipboard_bytes=excluded.max_clipboard_bytes,
                max_pixel_allocation_bytes=excluded.max_pixel_allocation_bytes,
                allowed_cidrs=excluded.allowed_cidrs, audit_labels=excluded.audit_labels,
                created_at=excluded.created_at, updated_at=excluded.updated_at
            """,
            (
                data["target_id"],
                data["endpoint"],
                data["tls_mode"],
                data["ca_secret_ref"],
                data["client_cert_secret_ref"],
                data["client_key_secret_ref"],
                data["expected_server_name"],
                _json(data["allowed_vm_patterns"]),
                data["tenant_id"],
                data["minimum_role"],
                data["connect_timeout_s"],
                data["handshake_timeout_s"],
                data["read_timeout_s"],
                data["write_timeout_s"],
                data["shutdown_timeout_s"],
                data["max_grpc_message_bytes"],
                data["max_framebuffer_width"],
                data["max_framebuffer_height"],
                data["max_rectangles"],
                data["max_clipboard_bytes"],
                data["max_pixel_allocation_bytes"],
                _json(data["allowed_cidrs"]),
                _json(data["audit_labels"]),
                data["created_at"],
                data["updated_at"],
            ),
        )

    @staticmethod
    def _record(row: Any) -> GraphicalTargetRecord:
        data = dict(row)
        data["allowed_vm_patterns"] = tuple(json.loads(data["allowed_vm_patterns"]))
        data["allowed_cidrs"] = tuple(json.loads(data["allowed_cidrs"]))
        data["audit_labels"] = tuple(tuple(pair) for pair in json.loads(data["audit_labels"]))
        return GraphicalTargetRecord(**data)

    async def get_graphical_target(self, target_id: str) -> GraphicalTargetRecord | None:
        cursor = await self._conn.execute("SELECT * FROM cp_graphical_targets WHERE target_id = ?", (target_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._record(row)

    async def list_graphical_targets(self) -> list[GraphicalTargetRecord]:
        cursor = await self._conn.execute("SELECT * FROM cp_graphical_targets ORDER BY target_id ASC")
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._record(row) for row in rows]

    async def delete_graphical_target(self, target_id: str) -> bool:
        cursor = await self._conn.execute("DELETE FROM cp_graphical_targets WHERE target_id = ?", (target_id,))
        deleted = cursor.rowcount > 0
        await cursor.close()
        return deleted
