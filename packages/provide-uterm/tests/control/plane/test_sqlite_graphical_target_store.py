#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, errors
from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord
from provide.uterm.control.plane.sqlite import SqliteControlPlane


class _ForeignTransaction:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _record(target_id: str, *, endpoint: str | None = None, updated_at: float = 2.0) -> GraphicalTargetRecord:
    return GraphicalTargetRecord(
        target_id=target_id,
        endpoint=endpoint or f"dns:///{target_id}.example:443",
        tls_mode="mtls",
        ca_secret_ref="file:/run/secrets/ca.pem",  # pragma: allowlist secret
        client_cert_secret_ref="env:CLIENT_CERT",  # pragma: allowlist secret
        client_key_secret_ref="file:/run/secrets/client.key",  # pragma: allowlist secret
        expected_server_name=f"{target_id}.example",
        allowed_vm_patterns=("prod-*", "shared-??"),
        tenant_id="tenant-1",
        minimum_role="operator",
        connect_timeout_s=5.0,
        handshake_timeout_s=10.0,
        read_timeout_s=30.0,
        write_timeout_s=15.0,
        shutdown_timeout_s=3.0,
        max_grpc_message_bytes=1_048_576,
        max_framebuffer_width=4096,
        max_framebuffer_height=2160,
        max_rectangles=1024,
        max_clipboard_bytes=65_536,
        max_pixel_allocation_bytes=35_389_440,
        allowed_cidrs=("203.0.113.0/24", "2001:db8::/32"),
        audit_labels=(("owner", "compute"), ("environment", "production")),
        created_at=1.0,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_sqlite_graphical_target_store_round_trip_replace_order_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "cp.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()
    first = _record("target-b")
    second = _record("target-a")
    replacement = _record("target-b", endpoint="dns:///replacement.example:443", updated_at=3.0)

    tx = await plane.begin()
    store = plane.graphical_target_store(tx)
    await store.put_graphical_target(first)
    await store.put_graphical_target(second)
    await store.put_graphical_target(replacement)
    assert await store.get_graphical_target("target-b") == replacement
    assert await store.list_graphical_targets() == [second, replacement]
    await tx.commit()
    await plane.close()

    reopened = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await reopened.migrate()
    read_tx = await reopened.begin()
    assert await reopened.graphical_target_store(read_tx).list_graphical_targets() == [second, replacement]
    await read_tx.rollback()
    await reopened.close()

    conn = sqlite3.connect(db_path)
    try:
        json_fields = conn.execute(
            "SELECT allowed_vm_patterns, allowed_cidrs, audit_labels FROM cp_graphical_targets "
            "WHERE target_id = 'target-b'"
        ).fetchone()
        stored = repr(conn.execute("SELECT * FROM cp_graphical_targets").fetchall())
    finally:
        conn.close()
    assert json_fields == (
        '["prod-*","shared-??"]',
        '["203.0.113.0/24","2001:db8::/32"]',
        '[["owner","compute"],["environment","production"]]',
    )
    assert "BEGIN CERTIFICATE" not in stored
    assert "PRIVATE KEY" not in stored


@pytest.mark.asyncio
async def test_sqlite_graphical_target_store_delete_and_rollback(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    tx = await plane.begin()
    await plane.graphical_target_store(tx).put_graphical_target(_record("kept"))
    await tx.commit()

    rollback_tx = await plane.begin()
    rollback_store = plane.graphical_target_store(rollback_tx)
    await rollback_store.put_graphical_target(_record("rolled-back"))
    assert await rollback_store.delete_graphical_target("kept") is True
    await rollback_tx.rollback()

    delete_tx = await plane.begin()
    delete_store = plane.graphical_target_store(delete_tx)
    assert await delete_store.get_graphical_target("rolled-back") is None
    assert await delete_store.delete_graphical_target("missing") is False
    assert await delete_store.delete_graphical_target("kept") is True
    await delete_tx.commit()

    read_tx = await plane.begin()
    assert await plane.graphical_target_store(read_tx).list_graphical_targets() == []
    await read_tx.rollback()
    await plane.close()


def test_sqlite_graphical_target_store_rejects_foreign_transaction() -> None:
    plane = SqliteControlPlane(ControlPlaneConfig())

    with pytest.raises(TypeError, match="SqliteTransaction"):
        plane.graphical_target_store(_ForeignTransaction())


@pytest.mark.asyncio
async def test_sqlite_graphical_target_store_uses_explicit_columns_across_schema_extension(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    assert plane._conn is not None
    await plane._conn.execute("ALTER TABLE cp_graphical_targets ADD COLUMN future_value TEXT NOT NULL DEFAULT 'future'")
    await plane._conn.commit()

    tx = await plane.begin()
    record = _record("extended")
    store = plane.graphical_target_store(tx)
    await store.put_graphical_target(record)
    assert await store.get_graphical_target("extended") == record
    await tx.rollback()
    await plane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "corrupt_value"),
    [
        ("allowed_vm_patterns", "{"),
        ("allowed_vm_patterns", b"[\xff]"),
        ("allowed_vm_patterns", '{"pattern":"prod-*"}'),
        ("allowed_vm_patterns", "[1]"),
        ("allowed_cidrs", '"203.0.113.0/24"'),
        ("audit_labels", "{"),
        ("audit_labels", b"[[\xff]]"),
        ("audit_labels", '"owner"'),
        ("audit_labels", '["owner"]'),
        ("audit_labels", '[["owner"]]'),
        ("audit_labels", '[[1,"compute"]]'),
    ],
)
async def test_sqlite_graphical_target_store_wraps_corrupt_json_data(
    tmp_path: Path, field: str, corrupt_value: object
) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    tx = await plane.begin()
    await plane.graphical_target_store(tx).put_graphical_target(_record("corrupt"))
    await tx.commit()
    assert plane._conn is not None
    update_sql = {
        "allowed_vm_patterns": "UPDATE cp_graphical_targets SET allowed_vm_patterns = ? WHERE target_id = ?",
        "allowed_cidrs": "UPDATE cp_graphical_targets SET allowed_cidrs = ? WHERE target_id = ?",
        "audit_labels": "UPDATE cp_graphical_targets SET audit_labels = ? WHERE target_id = ?",
    }
    await plane._conn.execute(update_sql[field], (corrupt_value, "corrupt"))
    await plane._conn.commit()

    read_tx = await plane.begin()
    try:
        with pytest.raises(errors.ControlPlaneDataError, match=rf"target 'corrupt'.*{field}"):
            await plane.graphical_target_store(read_tx).get_graphical_target("corrupt")
    finally:
        await read_tx.rollback()
        await plane.close()
