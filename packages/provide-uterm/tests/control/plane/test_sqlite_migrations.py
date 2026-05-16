#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.sqlite import SqliteControlPlane


@pytest.mark.asyncio
async def test_sqlite_migrate_bootstraps_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "control-plane.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))

    await plane.open()
    assert isinstance(plane._conn, aiosqlite.Connection)
    await plane.migrate()
    await plane.close()

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version_rows = list(conn.execute("SELECT version FROM cp_schema_version ORDER BY version"))
    finally:
        conn.close()

    assert "cp_schema_version" in tables
    assert "cp_sessions" in tables
    assert "cp_session_tokens" in tables
    assert "cp_resume_tokens" in tables
    assert "cp_approvals" in tables
    assert "cp_leases" in tables
    assert version_rows == [(1,)]
