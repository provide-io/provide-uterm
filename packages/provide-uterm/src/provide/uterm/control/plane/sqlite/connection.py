#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiosqlite


class SqliteConnectionError(RuntimeError):
    """Raised when a SQLite control-plane connection cannot be initialized."""


def resolve_database_path(database_url: str) -> str:
    """Resolve a SQLite database URL or filesystem path to a connectable path."""
    if database_url in {":memory:", "file::memory:"}:
        return ":memory:"
    parsed = urlparse(database_url)
    if parsed.scheme in {"sqlite", "sqlite+aiosqlite"}:
        path = unquote(parsed.path or "")
        if path in {"", "/:memory:", ":memory:"}:
            return ":memory:"
        if parsed.netloc and not path.startswith("/"):
            return f"//{parsed.netloc}{path}"
        return path
    return database_url


async def connect_sqlite(database_url: str, *, busy_timeout_ms: int = 5_000, wal: bool = True) -> aiosqlite.Connection:
    """Open a SQLite connection with the baseline bootstrap pragmas applied."""
    database_path = resolve_database_path(database_url)
    if database_path != ":memory:":
        Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(database_path)
    try:
        conn.row_factory = sqlite3.Row
        await conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal and database_path != ":memory:":
            await conn.execute("PRAGMA journal_mode=WAL")
        await conn.commit()
        return conn
    except Exception as exc:
        await conn.close()
        raise SqliteConnectionError(f"failed to initialize sqlite control-plane connection: {exc}") from exc
