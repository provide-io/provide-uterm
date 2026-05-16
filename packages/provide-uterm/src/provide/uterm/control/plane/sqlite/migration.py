from __future__ import annotations

import time
from typing import TYPE_CHECKING

from provide.uterm.control.plane.sqlite.schema.v0001_initial import SQL as V0001_SQL

if TYPE_CHECKING:
    import aiosqlite

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, V0001_SQL),)


class SqliteMigrationError(RuntimeError):
    """Raised when the SQLite control-plane schema cannot be migrated."""


async def apply_migrations(conn: aiosqlite.Connection, migration_table: str = "cp_schema_version") -> None:
    """Apply the inert control-plane schema migrations in order."""
    try:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {migration_table} (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        cursor = await conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM {migration_table}")
        row = await cursor.fetchone()
        await cursor.close()
        current_version = int(row[0] if row is not None and row[0] is not None else 0)
        for version, sql in MIGRATIONS:
            if version <= current_version:
                continue
            await conn.executescript(sql)
            await conn.execute(
                f"INSERT INTO {migration_table}(version, applied_at) VALUES(?, ?)",
                (version, time.time()),
            )
        await conn.commit()
    except Exception as exc:  # pragma: no cover - exercised via targeted failure tests later
        await conn.rollback()
        raise SqliteMigrationError(f"failed to apply control-plane migration: {exc}") from exc
