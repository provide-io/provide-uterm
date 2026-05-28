#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""list_pending ordering parity between memory and sqlite backends.

sqlite returns ``ORDER BY created_at ASC, approval_id ASC``; the memory
backend must return the same deterministic order so FIFO consumers behave
identically on both backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.approval.types import ApprovalRecord


def _config(backend: str, tmp_path: Path) -> ControlPlaneConfig:
    if backend == "sqlite":
        return ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "ord.db"))
    return ControlPlaneConfig(backend="memory")


def _approval(approval_id: str, created_at: float) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        session_id="s1",
        command="ls",
        requested_by=None,
        state="pending",
        created_at=created_at,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_list_pending_orders_by_created_at_then_id(backend: str, tmp_path: Path) -> None:
    plane = await bootstrap_control_plane(_config(backend, tmp_path))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.approval_store(tx)

    # Insert out of (created_at, approval_id) order, including a tie on
    # created_at that must break by approval_id.
    await store.put_approval(_approval("c", 30.0))
    await store.put_approval(_approval("a", 10.0))
    await store.put_approval(_approval("b-second", 20.0))
    await store.put_approval(_approval("b-first", 20.0))
    await tx.commit()

    read_tx = await plane.begin()
    pending = await plane.approval_store(read_tx).list_pending()
    await read_tx.rollback()
    await plane.close()

    ids = [r.approval_id for r in pending]
    assert ids == ["a", "b-first", "b-second", "c"]
