from __future__ import annotations

from pathlib import Path

import pytest

from provide.terminal.control.plane import ControlPlaneConfig
from provide.terminal.control.plane.approval import ApprovalRecord
from provide.terminal.control.plane.sqlite import SqliteControlPlane


@pytest.mark.asyncio
async def test_sqlite_approval_store_round_trip(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.approval_store(tx)

    record = ApprovalRecord(
        approval_id="a1",
        session_id="s1",
        command="rm -rf /tmp/demo",
        requested_by="alice",
        state="pending",
        created_at=1.0,
        resolved_at=None,
        resolved_by=None,
    )

    await store.put_approval(record)
    await tx.commit()

    tx2 = await plane.begin()
    store2 = plane.approval_store(tx2)
    fetched = await store2.get_approval("a1")
    pending = await store2.list_pending()
    await tx2.rollback()
    await plane.close()

    assert fetched == record
    assert [item.approval_id for item in pending] == ["a1"]
