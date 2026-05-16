from __future__ import annotations

from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.session import SessionRecord
from provide.uterm.control.plane.sqlite import SqliteControlPlane


@pytest.mark.asyncio
async def test_sqlite_session_store_round_trip(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.session_store(tx)

    record = SessionRecord(
        session_id="s1",
        display_name="Session One",
        connector_type="shell",
        owner="alice",
        visibility="private",
        lifecycle_state="waiting",
        created_at=1.0,
        updated_at=2.0,
        deleted_at=None,
    )

    await store.upsert_session(record)
    await tx.commit()

    tx2 = await plane.begin()
    fetched = await plane.session_store(tx2).get_session("s1")
    await tx2.rollback()
    await plane.close()

    assert fetched == record
