#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Transaction-isolation parity between the memory and sqlite backends.

Both backends advertise ``supports_transactions=True``; a lease-acquire
race that the sqlite serialization rejects must NOT double-grant on the
memory backend. The memory backend uses optimistic-concurrency conflict
detection at commit time to match sqlite's ``BEGIN IMMEDIATE`` behavior.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.control.plane.lease.types import LeaseRecord


def _make_plane_config(backend: str, tmp_path: Path) -> ControlPlaneConfig:
    if backend == "sqlite":
        return ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "iso.db"))
    return ControlPlaneConfig(backend="memory")


def _lease(session_id: str, owner: str) -> LeaseRecord:
    return LeaseRecord(
        session_id=session_id,
        hijack_id=f"h-{owner}",
        owner=owner,
        lease_expires_at=10.0,
        created_at=1.0,
        deleted_at=None,
    )


async def _prepare_plane(backend: str, tmp_path: Path) -> object:
    plane = await bootstrap_control_plane(_make_plane_config(backend, tmp_path))
    # sqlite needs its schema; memory's migrate is a no-op.
    await plane.migrate()
    return plane


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_concurrent_lease_acquire_single_winner(backend: str, tmp_path: Path) -> None:
    """Two transactions racing to acquire the same lease: exactly one wins.

    The two transactions are opened BEFORE either commits — for memory
    this means both snapshot the empty lease table (the double-grant
    window); for sqlite ``begin()`` is serialized by the held tx-lock and
    the second ``begin()`` only returns after the first commits. In both
    cases the invariant holds: at most one acquire succeeds.
    """
    plane = await _prepare_plane(backend, tmp_path)

    # Memory: both transactions begin (snapshotting the empty table)
    # before either commits. sqlite would deadlock if we held two open
    # transactions, so open the second only when the backend allows it.
    if backend == "memory":
        tx_a = await plane.begin()
        tx_b = await plane.begin()
        a_empty = await plane.lease_store(tx_a).get_lease("w1") is None
        b_empty = await plane.lease_store(tx_b).get_lease("w1") is None

        async def commit(tx: object, empty: bool, owner: str) -> bool:
            if not empty:
                await tx.rollback()
                return False
            try:
                await plane.lease_store(tx).put_lease(_lease("w1", owner))
                await tx.commit()
                return True
            except ControlPlaneConflictError:
                await tx.rollback()
                return False

        won_a = await commit(tx_a, a_empty, "a")
        won_b = await commit(tx_b, b_empty, "b")
        results = [won_a, won_b]
    else:

        async def acquire(owner: str) -> bool:
            tx = await plane.begin()
            try:
                if await plane.lease_store(tx).get_lease("w1") is None:
                    await plane.lease_store(tx).put_lease(_lease("w1", owner))
                    await tx.commit()
                    return True
                await tx.rollback()
                return False
            except ControlPlaneConflictError:
                await tx.rollback()
                return False

        results = list(await asyncio.gather(acquire("a"), acquire("b")))

    # Exactly one transaction may win the lease — never both.
    assert sum(results) == 1

    # The persisted lease is consistent with the single winner.
    read_tx = await plane.begin()
    final = await plane.lease_store(read_tx).get_lease("w1")
    await read_tx.rollback()
    await plane.close()
    assert final is not None
    assert final.owner in {"a", "b"}


@pytest.mark.asyncio
async def test_memory_non_conflicting_concurrent_writes_both_commit() -> None:
    # Two transactions writing *different* keys must not falsely conflict.
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    await plane.open()

    tx1 = await plane.begin()
    tx2 = await plane.begin()
    await plane.lease_store(tx1).put_lease(_lease("k1", "a"))
    await plane.lease_store(tx2).put_lease(_lease("k2", "b"))
    await tx2.commit()
    await tx1.commit()  # must not raise — disjoint keys

    read_tx = await plane.begin()
    store = plane.lease_store(read_tx)
    assert await store.get_lease("k1") is not None
    assert await store.get_lease("k2") is not None
    await read_tx.rollback()
    await plane.close()
