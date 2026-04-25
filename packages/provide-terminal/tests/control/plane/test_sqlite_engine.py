from __future__ import annotations

import aiosqlite
from pathlib import Path

import pytest

from provide.terminal.control.plane import ControlPlaneConfig
from provide.terminal.control.plane.sqlite import SqliteControlPlane
from provide.terminal.control.plane.sqlite.migration import SqliteMigrationError


@pytest.mark.asyncio
async def test_sqlite_control_plane_defaults_to_portable_capabilities(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))

    assert plane.capabilities.supports_transactions is True
    assert plane.capabilities.supports_migrations is True
    assert plane.capabilities.supports_retries is True

    await plane.open()
    assert isinstance(plane._conn, aiosqlite.Connection)
    tx = await plane.begin()
    assert tx.__class__.__name__ == "SqliteTransaction"
    assert plane.session_store(tx).__class__.__name__ == "SqliteSessionStore"
    assert plane.token_store(tx).__class__.__name__ == "SqliteTokenStore"
    assert plane.approval_store(tx).__class__.__name__ == "SqliteApprovalStore"
    assert plane.lease_store(tx).__class__.__name__ == "SqliteLeaseStore"
    await tx.rollback()
    await plane.close()


@pytest.mark.asyncio
async def test_sqlite_migrate_rejects_invalid_database_file(tmp_path: Path) -> None:
    db_path = tmp_path / "cp.db"
    db_path.write_text("not a sqlite database", encoding="utf-8")
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))

    with pytest.raises(SqliteMigrationError):
        await plane.migrate()

    await plane.close()
