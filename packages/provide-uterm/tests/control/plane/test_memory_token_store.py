from __future__ import annotations

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.token import ResumeTokenRecord


@pytest.mark.asyncio
async def test_memory_token_store_create_get_and_revoke_resume_token() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    await plane.open()
    tx = await plane.begin()
    store = plane.token_store(tx)

    record = ResumeTokenRecord(
        token_value="resume-1",
        session_id="worker-1",
        role="viewer",
        created_at=1.0,
        expires_at=2.0,
        was_hijack_owner=True,
        revoked_at=None,
    )

    await store.create_resume_token(record)
    await tx.commit()

    tx2 = await plane.begin()
    store2 = plane.token_store(tx2)
    fetched = await store2.get_resume_token("resume-1")
    await store2.revoke_resume_token("resume-1", 3.0)
    await tx2.commit()

    tx3 = await plane.begin()
    revoked = await plane.token_store(tx3).get_resume_token("resume-1")
    await tx3.rollback()
    await plane.close()

    assert fetched == record
    assert revoked is None
