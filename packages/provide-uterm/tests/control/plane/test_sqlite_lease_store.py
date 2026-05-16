#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.lease import LeaseRecord
from provide.uterm.control.plane.sqlite import SqliteControlPlane


@pytest.mark.asyncio
async def test_sqlite_lease_store_round_trip_and_clear(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.lease_store(tx)

    record = LeaseRecord(
        session_id="s1",
        hijack_id="h1",
        owner="alice",
        lease_expires_at=10.0,
        created_at=1.0,
        deleted_at=None,
    )

    await store.put_lease(record)
    await tx.commit()

    tx2 = await plane.begin()
    store2 = plane.lease_store(tx2)
    fetched = await store2.get_lease("s1")
    await store2.clear_lease("s1")
    await tx2.commit()

    tx3 = await plane.begin()
    cleared = await plane.lease_store(tx3).get_lease("s1")
    await tx3.rollback()
    await plane.close()

    assert fetched == record
    assert cleared is None
