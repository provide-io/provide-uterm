#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord

if TYPE_CHECKING:
    import sqlite3

    from provide.uterm.control.plane.sqlite.transaction import SqliteTransaction


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class SqliteGraphicalTargetStore:
    def __init__(self, tx: SqliteTransaction) -> None:
        self._conn = tx._conn

    async def put_graphical_target(self, record: GraphicalTargetRecord) -> None:
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
                record.target_id,
                record.endpoint,
                record.tls_mode,
                record.ca_secret_ref,
                record.client_cert_secret_ref,
                record.client_key_secret_ref,
                record.expected_server_name,
                _json(record.allowed_vm_patterns),
                record.tenant_id,
                record.minimum_role,
                record.connect_timeout_s,
                record.handshake_timeout_s,
                record.read_timeout_s,
                record.write_timeout_s,
                record.shutdown_timeout_s,
                record.max_grpc_message_bytes,
                record.max_framebuffer_width,
                record.max_framebuffer_height,
                record.max_rectangles,
                record.max_clipboard_bytes,
                record.max_pixel_allocation_bytes,
                _json(record.allowed_cidrs),
                _json(record.audit_labels),
                record.created_at,
                record.updated_at,
            ),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> GraphicalTargetRecord:
        return GraphicalTargetRecord(
            target_id=row["target_id"],
            endpoint=row["endpoint"],
            tls_mode=row["tls_mode"],
            ca_secret_ref=row["ca_secret_ref"],
            client_cert_secret_ref=row["client_cert_secret_ref"],
            client_key_secret_ref=row["client_key_secret_ref"],
            expected_server_name=row["expected_server_name"],
            allowed_vm_patterns=tuple(json.loads(row["allowed_vm_patterns"])),
            tenant_id=row["tenant_id"],
            minimum_role=row["minimum_role"],
            connect_timeout_s=row["connect_timeout_s"],
            handshake_timeout_s=row["handshake_timeout_s"],
            read_timeout_s=row["read_timeout_s"],
            write_timeout_s=row["write_timeout_s"],
            shutdown_timeout_s=row["shutdown_timeout_s"],
            max_grpc_message_bytes=row["max_grpc_message_bytes"],
            max_framebuffer_width=row["max_framebuffer_width"],
            max_framebuffer_height=row["max_framebuffer_height"],
            max_rectangles=row["max_rectangles"],
            max_clipboard_bytes=row["max_clipboard_bytes"],
            max_pixel_allocation_bytes=row["max_pixel_allocation_bytes"],
            allowed_cidrs=tuple(json.loads(row["allowed_cidrs"])),
            audit_labels=tuple(tuple(pair) for pair in json.loads(row["audit_labels"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
