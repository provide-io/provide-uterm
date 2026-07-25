#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Durable audit-chain head persistence on both control-plane backends.

The control-plane stores the latest audit-chain head ``(seq, record_hash)`` so a
restart can continue the chain and detect end-truncation / rollback.  The write
is MONOTONIC: a lower-or-equal seq is a no-op (the anti-rollback guard).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.bootstrap import ControlPlane
from provide.uterm.control.plane.memory import MemoryControlPlane
from provide.uterm.control.plane.sqlite import SqliteControlPlane


async def _memory_plane(tmp_path: Path) -> ControlPlane:
    plane = MemoryControlPlane(ControlPlaneConfig(backend="memory"))
    await plane.migrate()
    return plane


async def _sqlite_plane(tmp_path: Path) -> ControlPlane:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    return plane


@pytest.fixture(params=["memory", "sqlite"])
async def plane(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[ControlPlane]:
    factory = {"memory": _memory_plane, "sqlite": _sqlite_plane}[request.param]
    p = await factory(tmp_path)
    try:
        yield p
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_fresh_head_is_none(plane: ControlPlane) -> None:
    assert await plane.get_audit_head() is None


@pytest.mark.asyncio
async def test_set_then_get_round_trips(plane: ControlPlane) -> None:
    await plane.set_audit_head(1, "aa")
    assert await plane.get_audit_head() == (1, "aa")


@pytest.mark.asyncio
async def test_advancing_seq_moves_head_forward(plane: ControlPlane) -> None:
    await plane.set_audit_head(1, "aa")
    await plane.set_audit_head(2, "bb")
    assert await plane.get_audit_head() == (2, "bb")


@pytest.mark.asyncio
async def test_monotonic_guard_rejects_backward_and_equal(plane: ControlPlane) -> None:
    await plane.set_audit_head(2, "bb")
    # A lower seq must NOT roll the head back.
    await plane.set_audit_head(1, "zz")
    assert await plane.get_audit_head() == (2, "bb")
    # An equal seq is treated as already-recorded -> no-op (hash unchanged too).
    await plane.set_audit_head(2, "zz")
    assert await plane.get_audit_head() == (2, "bb")
    # A strictly greater seq advances normally.
    await plane.set_audit_head(3, "cc")
    assert await plane.get_audit_head() == (3, "cc")


@pytest.mark.asyncio
async def test_sqlite_head_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "cp.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()
    await plane.set_audit_head(7, "deadbeef")
    await plane.close()

    # A brand-new engine on the SAME db file must see the persisted head.
    reopened = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await reopened.migrate()
    assert await reopened.get_audit_head() == (7, "deadbeef")
    # The monotonic guard persists across the reopen too: a lower seq is a no-op.
    await reopened.set_audit_head(3, "zz")
    assert await reopened.get_audit_head() == (7, "deadbeef")
    await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_migration_creates_audit_head_table_at_v2(tmp_path: Path) -> None:
    db_path = tmp_path / "cp.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()
    await plane.close()

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        max_version = conn.execute("SELECT MAX(version) FROM cp_schema_version").fetchone()[0]
    finally:
        conn.close()

    assert "cp_audit_head" in tables
    assert max_version == 3


@pytest.mark.asyncio
async def test_sqlite_migrate_v1_db_forward_to_head(tmp_path: Path) -> None:
    """A db already at v1 (no audit-head table) migrates forward to head cleanly."""
    db_path = tmp_path / "cp.db"
    from provide.uterm.control.plane.sqlite import connect_sqlite
    from provide.uterm.control.plane.sqlite.schema.v0001_initial import SQL as V0001_SQL

    # Hand-build a db that is only at schema version 1.
    conn = await connect_sqlite(str(db_path))
    await conn.executescript(V0001_SQL)
    await conn.execute("INSERT INTO cp_schema_version(version, applied_at) VALUES(1, 0.0)")
    await conn.commit()
    await conn.close()

    # Re-migrate via the normal path: v1 is skipped, v2 and v3 are applied.
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()
    await plane.set_audit_head(5, "ff")
    assert await plane.get_audit_head() == (5, "ff")
    await plane.close()

    db = sqlite3.connect(db_path)
    try:
        versions = sorted(row[0] for row in db.execute("SELECT version FROM cp_schema_version"))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()
    assert versions == [1, 2, 3]
    assert "cp_audit_head" in tables
    assert "cp_graphical_targets" in tables


@pytest.mark.asyncio
async def test_sqlite_set_audit_head_rolls_back_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the upsert raises, set_audit_head rolls back, re-raises, frees the lock."""
    db_path = tmp_path / "cp.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()
    assert plane._conn is not None
    real_execute = plane._conn.execute

    async def _boom(sql: str, *args: object, **kwargs: object) -> object:
        if sql.startswith("INSERT INTO cp_audit_head"):
            raise RuntimeError("boom")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(plane._conn, "execute", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        await plane.set_audit_head(1, "aa")

    # Lock must be released and nothing persisted.
    monkeypatch.undo()
    assert await plane.get_audit_head() is None
    await plane.close()
