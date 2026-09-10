#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiosqlite

# sqlite:///C:/Users/... yields urlparse path "/C:/Users/..." — a leading "/"
# before a Windows drive letter, which Path() treats as a literal (invalid)
# folder segment rather than a drive-anchored path. The lookahead (rather than
# consuming the separator) also matches a bare drive with no trailing segment
# ("sqlite:///C:" -> path "/C:"), not just "/C:/...".
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^/[A-Za-z]:(?=[/\\]|$)")

# sqlite://C:/Users/... (2 slashes, missing the 3rd) puts the drive letter in
# netloc instead of path ("C:", "/Users/...") -- a malformed-but-common typo
# of the 3-slash form, not a real host, so it's special-cased ahead of the
# general netloc handling below.
_DRIVE_LETTER_NETLOC_RE = re.compile(r"^[A-Za-z]:$")


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
        if _DRIVE_LETTER_NETLOC_RE.match(parsed.netloc):
            return f"{parsed.netloc}{path}"
        if _WINDOWS_DRIVE_PATH_RE.match(path):
            path = path[1:]
        if parsed.netloc:
            return f"//{parsed.netloc}{path}" if path.startswith("/") else f"//{parsed.netloc}/{path}"
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
