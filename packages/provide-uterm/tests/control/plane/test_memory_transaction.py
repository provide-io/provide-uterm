#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import time

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.session.types import SessionRecord


@pytest.mark.asyncio
async def test_memory_transaction_rollback_reverts_state() -> None:
    config = ControlPlaneConfig(backend="memory")
    plane = await bootstrap_control_plane(config)

    # 1. Start a transaction
    tx = await plane.begin()
    store = plane.session_store(tx)

    session_id = "test-session"
    record = SessionRecord(
        session_id=session_id,
        display_name="Test Session",
        connector_type="pty",
        owner="user",
        visibility="private",
        lifecycle_state="waiting",
        created_at=time.time(),
        updated_at=time.time(),
    )

    # 2. Add a record
    await store.upsert_session(record)
    assert await store.get_session(session_id) == record

    # 3. Rollback
    await tx.rollback()

    # 4. Verify state reverted
    new_tx = await plane.begin()
    new_store = plane.session_store(new_tx)
    assert await new_store.get_session(session_id) is None


@pytest.mark.asyncio
async def test_memory_transaction_commit_persists_state() -> None:
    config = ControlPlaneConfig(backend="memory")
    plane = await bootstrap_control_plane(config)

    # 1. Start a transaction
    tx = await plane.begin()
    store = plane.session_store(tx)

    session_id = "test-session-commit"
    record = SessionRecord(
        session_id=session_id,
        display_name="Test Session Commit",
        connector_type="pty",
        owner="user",
        visibility="private",
        lifecycle_state="waiting",
        created_at=time.time(),
        updated_at=time.time(),
    )

    # 2. Add a record
    await store.upsert_session(record)

    # 3. Commit
    await tx.commit()

    # 4. Verify state persisted
    new_tx = await plane.begin()
    new_store = plane.session_store(new_tx)
    assert await new_store.get_session(session_id) == record


@pytest.mark.asyncio
async def test_memory_transaction_rollback_does_not_revert_committed_concurrent_transaction() -> None:
    config = ControlPlaneConfig(backend="memory")
    plane = await bootstrap_control_plane(config)

    tx1 = await plane.begin()
    tx2 = await plane.begin()

    store2 = plane.session_store(tx2)
    record = SessionRecord(
        session_id="committed-while-first-open",
        display_name="Committed Concurrent Session",
        connector_type="pty",
        owner="user",
        visibility="private",
        lifecycle_state="waiting",
        created_at=time.time(),
        updated_at=time.time(),
    )
    await store2.upsert_session(record)
    await tx2.commit()

    await tx1.rollback()

    read_tx = await plane.begin()
    read_store = plane.session_store(read_tx)
    assert await read_store.get_session(record.session_id) == record
