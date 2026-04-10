#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for FanOutStore protocol and InMemoryFanOutStore implementation."""

from __future__ import annotations

from provide.terminal.bridge.fanout._models import FanOutGroup
from provide.terminal.bridge.fanout._store import InMemoryFanOutStore


def _make_group(
    group_id: str = "g1",
    name: str = "Test Group",
    worker_ids: list[str] | None = None,
    created_by: str = "user1",
    created_at: float = 1000.0,
    grants: list[str] | None = None,
) -> FanOutGroup:
    return FanOutGroup(
        group_id=group_id,
        name=name,
        worker_ids=worker_ids if worker_ids is not None else [],
        created_by=created_by,
        created_at=created_at,
        grants=grants if grants is not None else [],
    )


class TestInMemoryFanOutStoreCRUD:
    async def test_save_and_get(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1")
        await store.save(group)
        result = await store.get("g1")
        assert result is not None
        assert result.group_id == "g1"

    async def test_get_nonexistent_returns_none(self) -> None:
        store = InMemoryFanOutStore()
        result = await store.get("does-not-exist")
        assert result is None

    async def test_save_overwrites_existing(self) -> None:
        store = InMemoryFanOutStore()
        group_v1 = _make_group(group_id="g1", name="Original")
        group_v2 = _make_group(group_id="g1", name="Updated")
        await store.save(group_v1)
        await store.save(group_v2)
        result = await store.get("g1")
        assert result is not None
        assert result.name == "Updated"

    async def test_delete_existing(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1")
        await store.save(group)
        await store.delete("g1")
        result = await store.get("g1")
        assert result is None

    async def test_delete_nonexistent_no_error(self) -> None:
        store = InMemoryFanOutStore()
        # Must not raise
        await store.delete("does-not-exist")

    async def test_multiple_groups_independent(self) -> None:
        store = InMemoryFanOutStore()
        g1 = _make_group(group_id="g1", name="Group One")
        g2 = _make_group(group_id="g2", name="Group Two")
        await store.save(g1)
        await store.save(g2)
        r1 = await store.get("g1")
        r2 = await store.get("g2")
        assert r1 is not None and r1.name == "Group One"
        assert r2 is not None and r2.name == "Group Two"


class TestInMemoryFanOutStoreListForPrincipal:
    async def test_creator_sees_own_group(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1", created_by="alice")
        await store.save(group)
        results = await store.list_for_principal("alice")
        assert len(results) == 1
        assert results[0].group_id == "g1"

    async def test_granted_principal_sees_group(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1", created_by="alice", grants=["bob", "carol"])
        await store.save(group)
        results = await store.list_for_principal("bob")
        assert len(results) == 1
        assert results[0].group_id == "g1"

    async def test_unrelated_principal_excluded(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1", created_by="alice", grants=["carol"])
        await store.save(group)
        results = await store.list_for_principal("dave")
        assert results == []

    async def test_principal_sees_created_and_granted(self) -> None:
        store = InMemoryFanOutStore()
        own_group = _make_group(group_id="g1", created_by="alice")
        granted_group = _make_group(group_id="g2", created_by="bob", grants=["alice"])
        other_group = _make_group(group_id="g3", created_by="carol")
        await store.save(own_group)
        await store.save(granted_group)
        await store.save(other_group)
        results = await store.list_for_principal("alice")
        ids = {r.group_id for r in results}
        assert ids == {"g1", "g2"}

    async def test_list_empty_store_returns_empty(self) -> None:
        store = InMemoryFanOutStore()
        results = await store.list_for_principal("alice")
        assert results == []

    async def test_deleted_group_not_listed(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1", created_by="alice")
        await store.save(group)
        await store.delete("g1")
        results = await store.list_for_principal("alice")
        assert results == []

    async def test_second_grantee_also_sees_group(self) -> None:
        store = InMemoryFanOutStore()
        group = _make_group(group_id="g1", created_by="alice", grants=["bob", "carol"])
        await store.save(group)
        carol_results = await store.list_for_principal("carol")
        assert len(carol_results) == 1
        assert carol_results[0].group_id == "g1"
