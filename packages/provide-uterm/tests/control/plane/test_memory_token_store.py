#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
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


@pytest.mark.asyncio
async def test_memory_token_store_consume_resume_token_single_use() -> None:
    """consume_resume_token returns the record on first call, None on second (already revoked)."""
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    await plane.open()

    record = ResumeTokenRecord(
        token_value="consume-test",
        session_id="worker-1",
        role="admin",
        created_at=1.0,
        expires_at=9999.0,
        was_hijack_owner=False,
        revoked_at=None,
    )

    tx = await plane.begin()
    await plane.token_store(tx).create_resume_token(record)
    await tx.commit()

    # First consume: should return the record
    tx2 = await plane.begin()
    result = await plane.token_store(tx2).consume_resume_token("consume-test", 2.0)
    await tx2.commit()
    assert result is not None
    assert result.token_value == "consume-test"

    # Second consume: already revoked, returns None
    tx3 = await plane.begin()
    result2 = await plane.token_store(tx3).consume_resume_token("consume-test", 3.0)
    await tx3.commit()
    assert result2 is None

    await plane.close()


@pytest.mark.asyncio
async def test_memory_token_store_consume_nonexistent_returns_none() -> None:
    """consume_resume_token on a non-existent token returns None."""
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    await plane.open()
    tx = await plane.begin()
    result = await plane.token_store(tx).consume_resume_token("no-such-token", 1.0)
    await tx.commit()
    assert result is None
    await plane.close()
