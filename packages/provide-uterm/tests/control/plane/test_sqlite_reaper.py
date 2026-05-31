#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.sqlite import SqliteControlPlane

# A fixed "now" so retention math is deterministic.  retention_s=100 => cutoff=900.
NOW = 1000.0
RETENTION_S = 100
CUTOFF = NOW - RETENTION_S  # 900.0


async def _insert(plane: SqliteControlPlane, sql: str, params: tuple[object, ...]) -> None:
    tx = await plane.begin()
    assert plane._conn is not None
    await plane._conn.execute(sql, params)
    await tx.commit()


def _count(db_path: Path, table: str) -> int:
    db = sqlite3.connect(str(db_path))
    try:
        return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608 - table is a literal
    finally:
        db.close()


async def _new_plane(tmp_path: Path) -> tuple[SqliteControlPlane, Path]:
    db_path = tmp_path / "cp.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()
    return plane, db_path


@pytest.mark.asyncio
async def test_reap_resume_tokens_revoked_and_expired(tmp_path: Path) -> None:
    plane, db_path = await _new_plane(tmp_path)
    # Revoked past retention -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("rev-old", "s1", "viewer", 0.0, NOW + 10_000, CUTOFF - 1),
    )
    # Recently revoked (revoked_at >= cutoff) -> survives.
    await _insert(
        plane,
        "INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("rev-new", "s1", "viewer", 0.0, NOW + 10_000, CUTOFF),
    )
    # Naturally expired (expires_at < cutoff, revoked_at NULL) -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("exp-old", "s1", "viewer", 0.0, CUTOFF - 1, None),
    )
    # Still valid (expires_at >= cutoff, revoked_at NULL) -> survives.
    await _insert(
        plane,
        "INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("valid", "s1", "viewer", 0.0, NOW + 10_000, None),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 2
    assert _count(db_path, "cp_resume_tokens") == 2
    db = sqlite3.connect(str(db_path))
    survivors = {row[0] for row in db.execute("SELECT token_value FROM cp_resume_tokens")}
    db.close()
    assert survivors == {"rev-new", "valid"}


@pytest.mark.asyncio
async def test_reap_session_tokens(tmp_path: Path) -> None:
    plane, db_path = await _new_plane(tmp_path)
    # Expired session token -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("s1", "operator", "v1", 0.0, CUTOFF - 1, None),
    )
    # expires_at NULL (never expires) and old created_at -> survives.
    await _insert(
        plane,
        "INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("s2", "operator", "v2", 0.0, None, None),
    )
    # Revoked past retention -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("s3", "operator", "v3", 0.0, None, CUTOFF - 1),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 2
    db = sqlite3.connect(str(db_path))
    survivors = {row[0] for row in db.execute("SELECT session_id FROM cp_session_tokens")}
    db.close()
    assert survivors == {"s2"}


@pytest.mark.asyncio
async def test_reap_soft_deleted_sessions(tmp_path: Path) -> None:
    plane, db_path = await _new_plane(tmp_path)
    # Soft-deleted past retention -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_sessions(session_id, display_name, connector_type, visibility, lifecycle_state, "
        "created_at, updated_at, deleted_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        ("dead", "d", "shell", "private", "stopped", 0.0, 0.0, CUTOFF - 1),
    )
    # Live session (deleted_at NULL) -> survives.
    await _insert(
        plane,
        "INSERT INTO cp_sessions(session_id, display_name, connector_type, visibility, lifecycle_state, "
        "created_at, updated_at, deleted_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        ("live", "l", "shell", "private", "running", 0.0, 0.0, None),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 1
    assert _count(db_path, "cp_sessions") == 1


@pytest.mark.asyncio
async def test_reap_soft_deleted_leases(tmp_path: Path) -> None:
    plane, db_path = await _new_plane(tmp_path)
    # Soft-deleted lease past retention -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("dead", "h1", "alice", NOW + 10_000, 0.0, CUTOFF - 1),
    )
    # Lease that expired via lease_expires_at (no explicit clear -> deleted_at NULL)
    # past retention -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("expired", "h3", "carol", CUTOFF - 1, 0.0, None),
    )
    # Live lease (future lease_expires_at, never cleared) -> survives.
    await _insert(
        plane,
        "INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("live", "h2", "bob", NOW + 10_000, 0.0, None),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 2
    assert _count(db_path, "cp_leases") == 1
    db = sqlite3.connect(str(db_path))
    survivors = {row[0] for row in db.execute("SELECT session_id FROM cp_leases")}
    db.close()
    assert survivors == {"live"}


@pytest.mark.asyncio
async def test_reap_resolved_approvals(tmp_path: Path) -> None:
    plane, db_path = await _new_plane(tmp_path)
    # Resolved past retention -> deleted.
    await _insert(
        plane,
        "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at, resolved_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("a-old", "s1", "rm -rf", "approved", 0.0, CUTOFF - 1),
    )
    # Unresolved (resolved_at NULL) -> survives.
    await _insert(
        plane,
        "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at, resolved_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("a-pending", "s1", "ls", "pending", 0.0, None),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 1
    assert _count(db_path, "cp_approvals") == 1


@pytest.mark.asyncio
async def test_reap_boundary_is_strict(tmp_path: Path) -> None:
    """A row whose timestamp == cutoff is NOT deleted (strict ``<``)."""
    plane, db_path = await _new_plane(tmp_path)
    await _insert(
        plane,
        "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at, resolved_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("at-cutoff", "s1", "ls", "approved", 0.0, CUTOFF),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 0
    assert _count(db_path, "cp_approvals") == 1


@pytest.mark.asyncio
async def test_reap_returns_total_across_tables(tmp_path: Path) -> None:
    plane, _db_path = await _new_plane(tmp_path)
    await _insert(
        plane,
        "INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at, expires_at, revoked_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("rt", "s1", "viewer", 0.0, CUTOFF - 1, None),
    )
    await _insert(
        plane,
        "INSERT INTO cp_sessions(session_id, display_name, connector_type, visibility, lifecycle_state, "
        "created_at, updated_at, deleted_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        ("dead", "d", "shell", "private", "stopped", 0.0, 0.0, CUTOFF - 1),
    )
    await _insert(
        plane,
        "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at, resolved_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        ("a", "s1", "ls", "approved", 0.0, CUTOFF - 1),
    )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 3


@pytest.mark.asyncio
async def test_reap_truncates_wal_on_real_file(tmp_path: Path) -> None:
    """On a real-file WAL DB, reap runs the checkpoint without error and keeps the WAL bounded."""
    plane, db_path = await _new_plane(tmp_path)
    # Insert many soft-deleted rows so there's WAL content to truncate.
    for i in range(200):
        await _insert(
            plane,
            "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at, resolved_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (f"a-{i}", "s1", "ls", "approved", 0.0, CUTOFF - 1),
        )

    deleted = await plane.reap(now=NOW, retention_s=RETENTION_S)
    await plane.close()

    assert deleted == 200
    assert _count(db_path, "cp_approvals") == 0
    # After a TRUNCATE checkpoint the WAL sidecar should be empty (size 0) if it still exists.
    wal = Path(str(db_path) + "-wal")
    if wal.exists():
        assert wal.stat().st_size == 0


@pytest.mark.asyncio
async def test_reap_rolls_back_and_releases_lock_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a DELETE raises, reap rolls back, re-raises, and leaves the tx lock free."""
    plane, _db_path = await _new_plane(tmp_path)
    assert plane._conn is not None
    real_execute = plane._conn.execute

    async def _boom(sql: str, *args: object, **kwargs: object) -> object:
        if sql.startswith("DELETE"):
            raise RuntimeError("boom")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(plane._conn, "execute", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        await plane.reap(now=NOW, retention_s=RETENTION_S)

    # Lock must be released: a subsequent begin() must not deadlock.
    monkeypatch.undo()
    tx = await plane.begin()
    await tx.rollback()
    await plane.close()
