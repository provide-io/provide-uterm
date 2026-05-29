#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted coverage for control-plane branches not exercised elsewhere.

These tests close the perimeter gap created by widening the package
coverage source to include the whole ``control/`` subpackage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.approval.types import ApprovalRecord
from provide.uterm.control.plane.lease.types import LeaseRecord
from provide.uterm.control.plane.session.types import SessionRecord
from provide.uterm.control.plane.sqlite import SqliteControlPlane
from provide.uterm.control.plane.sqlite.connection import (
    SqliteConnectionError,
    connect_sqlite,
    resolve_database_path,
)
from provide.uterm.control.plane.sqlite.migration import SqliteMigrationError, apply_migrations
from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord

# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_rejects_unknown_backend() -> None:
    bad = ControlPlaneConfig()
    # Frozen dataclass — override the backend to an unsupported value.
    object.__setattr__(bad, "backend", "redis")
    with pytest.raises(ValueError, match="unsupported control-plane backend: redis"):
        await bootstrap_control_plane(bad)


# ---------------------------------------------------------------------------
# memory stores
# ---------------------------------------------------------------------------


def _session(session_id: str) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        display_name="n",
        connector_type="pty",
        owner="u",
        visibility="private",
        lifecycle_state="waiting",
        created_at=1.0,
        updated_at=1.0,
    )


async def test_memory_session_store_get_and_mark_deleted() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    tx = await plane.begin()
    store = plane.session_store(tx)
    # mark_deleted on a missing session is a no-op (early return branch).
    await store.mark_deleted("missing", 5.0)
    await store.upsert_session(_session("s1"))
    fetched = await store.get_session("s1")
    assert fetched is not None
    await store.mark_deleted("s1", 5.0)
    deleted = await store.get_session("s1")
    assert deleted is not None
    assert deleted.lifecycle_state == "deleted"
    assert deleted.deleted_at == 5.0
    await tx.rollback()


async def test_memory_approval_store_get_and_lease_clear() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    tx = await plane.begin()
    approvals = plane.approval_store(tx)
    record = ApprovalRecord(
        approval_id="a1",
        session_id="s1",
        command="ls",
        requested_by=None,
        state="pending",
        created_at=1.0,
    )
    await approvals.put_approval(record)
    assert await approvals.get_approval("a1") == record
    assert await approvals.get_approval("missing") is None

    leases = plane.lease_store(tx)
    lease = LeaseRecord(session_id="s1", hijack_id="h", owner="u", lease_expires_at=9.0, created_at=1.0)
    await leases.put_lease(lease)
    assert await leases.get_lease("s1") == lease
    await leases.clear_lease("s1")
    assert await leases.get_lease("s1") is None
    await tx.rollback()


async def test_memory_token_store_session_token_and_revoke_missing() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    tx = await plane.begin()
    store = plane.token_store(tx)
    token = SessionTokenRecord(
        session_id="s1",
        token_kind="resume",
        token_value="tok",
        created_at=1.0,
        expires_at=2.0,
    )
    await store.put_session_token(token)
    assert await store.get_session_token("s1", "resume") == token
    assert await store.get_session_token("s1", "missing") is None
    # Revoking a non-existent resume token is a no-op (early return branch).
    await store.revoke_resume_token("nope", 3.0)
    await tx.rollback()


# ---------------------------------------------------------------------------
# memory transaction edge cases
# ---------------------------------------------------------------------------


async def test_memory_commit_is_idempotent_after_close() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    tx = await plane.begin()
    await plane.session_store(tx).upsert_session(_session("s1"))
    await tx.commit()
    # Second commit hits the `if self.closed: return` early-out branch.
    await tx.commit()


async def test_memory_commit_applies_key_deletion() -> None:
    # Exercise the delete branch of _merge_table (key removed in working copy).
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    seed = await plane.begin()
    await plane.session_store(seed).upsert_session(_session("s1"))
    await seed.commit()

    tx = await plane.begin()
    # Remove the key from the working state, then commit — root must drop it.
    tx.state.sessions.pop("s1", None)
    await tx.commit()

    read = await plane.begin()
    assert await plane.session_store(read).get_session("s1") is None
    await read.rollback()


# ---------------------------------------------------------------------------
# sqlite connection / migration helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (":memory:", ":memory:"),
        ("file::memory:", ":memory:"),
        ("sqlite://:memory:", ":memory:"),
        ("/tmp/plain/path.db", "/tmp/plain/path.db"),
    ],
)
def test_resolve_database_path_variants(url: str, expected: str) -> None:
    assert resolve_database_path(url) == expected


def test_resolve_database_path_absolute_sqlite_scheme(tmp_path: Path) -> None:
    target = tmp_path / "db.sqlite"
    assert resolve_database_path(f"sqlite:///{target}") == f"/{target}"


async def test_connect_sqlite_creates_parent_and_wal(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "cp.db"
    conn = await connect_sqlite(str(db), wal=True)
    try:
        assert db.parent.is_dir()
    finally:
        await conn.close()


async def test_connect_sqlite_memory_skips_wal(tmp_path: Path) -> None:
    # In-memory connection takes the False side of the path/WAL branches.
    conn = await connect_sqlite(":memory:", wal=True)
    await conn.close()


async def test_apply_migrations_rejects_bad_table_name(tmp_path: Path) -> None:
    conn = await connect_sqlite(str(tmp_path / "m.db"))
    try:
        with pytest.raises(SqliteMigrationError, match="invalid migration table name"):
            await apply_migrations(conn, migration_table="bad name!")
    finally:
        await conn.close()


async def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    # Running migrations twice exercises the `version <= current_version`
    # skip branch on the second pass.
    conn = await connect_sqlite(str(tmp_path / "twice.db"))
    try:
        await apply_migrations(conn)
        await apply_migrations(conn)  # all versions already applied → skip branch
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# sqlite stores — None lookup branches + begin lock release
# ---------------------------------------------------------------------------


async def test_sqlite_store_missing_lookups_return_none(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "n.db")))
    await plane.migrate()
    tx = await plane.begin()
    assert await plane.session_store(tx).get_session("missing") is None
    assert await plane.token_store(tx).get_session_token("missing", "resume") is None
    assert await plane.token_store(tx).get_resume_token("missing") is None
    assert await plane.approval_store(tx).get_approval("missing") is None
    assert await plane.lease_store(tx).get_lease("missing") is None
    await tx.rollback()
    await plane.close()


async def test_sqlite_session_mark_deleted_and_round_trip(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "s.db")))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.session_store(tx)
    await store.upsert_session(_session("s1"))
    await tx.commit()

    tx2 = await plane.begin()
    store2 = plane.session_store(tx2)
    await store2.mark_deleted("s1", 9.0)
    await tx2.commit()

    tx3 = await plane.begin()
    got = await plane.session_store(tx3).get_session("s1")
    await tx3.rollback()
    await plane.close()
    assert got is not None
    assert got.lifecycle_state == "deleted"
    assert got.deleted_at == 9.0


async def test_sqlite_resume_token_revoked_returns_none(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "t.db")))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.token_store(tx)
    record = ResumeTokenRecord(
        token_value="r1",
        session_id="s1",
        role="viewer",
        created_at=1.0,
        expires_at=2.0,
        was_hijack_owner=True,
        revoked_at=None,
    )
    await store.create_resume_token(record)
    await store.revoke_resume_token("r1", 3.0)
    await tx.commit()

    tx2 = await plane.begin()
    # Revoked token resolves to None (the revoked_at branch).
    assert await plane.token_store(tx2).get_resume_token("r1") is None
    await tx2.rollback()
    await plane.close()


async def test_sqlite_begin_releases_lock_on_rollback(tmp_path: Path) -> None:
    # Two sequential transactions confirm the tx-lock is released by both
    # commit and rollback (begin would block forever otherwise).
    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "l.db")))
    await plane.migrate()
    tx = await plane.begin()
    await tx.rollback()
    tx2 = await plane.begin()
    await tx2.commit()
    tx3 = await plane.begin()
    await tx3.rollback()
    await plane.close()


async def test_sqlite_migrate_reraises_migration_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import provide.uterm.control.plane.sqlite.engine as engine_mod

    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "e.db")))

    async def boom(_conn: object) -> None:
        raise SqliteMigrationError("boom")

    monkeypatch.setattr(engine_mod, "apply_migrations", boom)
    with pytest.raises(SqliteMigrationError, match="boom"):
        await plane.migrate()
    await plane.close()


async def test_sqlite_migrate_wraps_connection_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import provide.uterm.control.plane.sqlite.engine as engine_mod

    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "e2.db")))

    async def boom(_conn: object) -> None:
        raise SqliteConnectionError("conn-fail")

    monkeypatch.setattr(engine_mod, "apply_migrations", boom)
    with pytest.raises(SqliteMigrationError, match="failed to apply control-plane migration"):
        await plane.migrate()
    await plane.close()


async def test_sqlite_begin_releases_lock_when_begin_immediate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "b.db")))
    await plane.migrate()

    original_execute = plane._conn.execute  # type: ignore[union-attr]

    async def failing_execute(sql: str, *args: object, **kwargs: object) -> object:
        if sql == "BEGIN IMMEDIATE":
            raise RuntimeError("begin failed")
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(plane._conn, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="begin failed"):
        await plane.begin()
    # The tx-lock must have been released so a later begin can succeed.
    monkeypatch.undo()
    tx = await plane.begin()
    await tx.rollback()
    await plane.close()


async def test_sqlite_release_lock_skips_when_already_unlocked(tmp_path: Path) -> None:
    # The `_release_lock` closure guards a double-release with
    # `if self._tx_lock.locked()`. Force the lock free before commit so the
    # closure takes the False branch.
    plane = SqliteControlPlane(ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "d.db")))
    await plane.migrate()
    tx = await plane.begin()
    # Release the tx-lock out-of-band so the on_close closure sees it unlocked.
    plane._tx_lock.release()
    await tx.commit()  # _release_lock runs but lock already free → False branch
    await plane.close()


async def test_sqlite_transaction_idempotent_and_no_on_close(tmp_path: Path) -> None:
    from provide.uterm.control.plane.sqlite.connection import connect_sqlite
    from provide.uterm.control.plane.sqlite.transaction import SqliteTransaction

    conn = await connect_sqlite(str(tmp_path / "tx.db"))
    try:
        # on_close=None exercises the `if self._on_close is not None` False branch.
        tx = SqliteTransaction(conn, on_close=None)
        await tx.commit()
        # Second commit hits the `if not self._closed` early-exit branch.
        await tx.commit()

        tx2 = SqliteTransaction(conn, on_close=None)
        await tx2.rollback()
        # Second rollback hits the early-exit branch.
        await tx2.rollback()
    finally:
        await conn.close()
