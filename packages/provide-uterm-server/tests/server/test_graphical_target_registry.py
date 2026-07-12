#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Merged static and runtime graphical-target registry tests."""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition
from provide.uterm.server.graphical import (
    GraphicalTargetAlreadyExistsError,
    GraphicalTargetClosedError,
    GraphicalTargetForbiddenError,
    GraphicalTargetImmutableError,
    GraphicalTargetNotFoundError,
    GraphicalTargetRegistry,
    GraphicalTargetScope,
    GraphicalTargetTransactionError,
)
from provide.uterm.server.secrets import SecretReference

SYSTEM = GraphicalTargetScope.system()
TENANT_ONE = GraphicalTargetScope.tenant("one")
TENANT_TWO = GraphicalTargetScope.tenant("two")


def test_graphical_target_scope_rejects_ambiguous_privilege() -> None:
    with pytest.raises(ValueError, match="tenant scope requires exactly one tenant_id"):
        GraphicalTargetScope("system", "one")


def _target(target_id: str, *, tenant_id: str | None = None, endpoint: str | None = None) -> GraphicalTargetDefinition:
    return GraphicalTargetDefinition(
        target_id=target_id,
        endpoint=endpoint or f"dns:///{target_id}.example:443",
        tenant_id=tenant_id,
        ca_secret_ref=SecretReference.parse("env:GRAPHICAL_CA"),
    )


async def _registry(
    backend: Literal["memory", "sqlite"],
    tmp_path: Path,
    *static: GraphicalTargetDefinition,
) -> GraphicalTargetRegistry:
    plane = await bootstrap_control_plane(
        ControlPlaneConfig(backend=backend, database_url=str(tmp_path / f"{backend}.db")),
    )
    await plane.migrate()
    return GraphicalTargetRegistry(tuple(static), plane, owns_control_plane=True)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_registry_merges_static_and_runtime_targets_deterministically(
    backend: Literal["memory", "sqlite"],
    tmp_path: Path,
) -> None:
    static = _target("static", tenant_id="one")
    registry = await _registry(backend, tmp_path, static)
    try:
        created = await registry.create(TENANT_ONE, _target("runtime", tenant_id="one"))

        assert created == _target("runtime", tenant_id="one")
        assert await registry.get(TENANT_ONE, "static") == static
        assert await registry.get(TENANT_ONE, "runtime") == created
        assert await registry.get(TENANT_TWO, "runtime") is None
        assert [target.target_id for target in await registry.list(SYSTEM)] == ["runtime", "static"]
        assert [target.target_id for target in await registry.list(TENANT_ONE)] == ["runtime", "static"]
        assert await registry.list(TENANT_TWO) == []
        assert await registry.get(TENANT_TWO, "static") is None
    finally:
        await registry.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_registry_runtime_crud_preserves_creation_time(
    backend: Literal["memory", "sqlite"],
    tmp_path: Path,
) -> None:
    registry = await _registry(backend, tmp_path)
    try:
        first = _target("runtime")
        await registry.create(SYSTEM, first)
        record_before = await registry.get_runtime_record(SYSTEM, "runtime")
        updated = _target("runtime", endpoint="dns:///changed.example:443")

        assert await registry.update(SYSTEM, updated) == updated
        record_after = await registry.get_runtime_record(SYSTEM, "runtime")
        assert record_before is not None and record_after is not None
        assert record_after.created_at == record_before.created_at
        assert record_after.updated_at >= record_before.updated_at
        await registry.delete(SYSTEM, "runtime")
        assert await registry.get(SYSTEM, "runtime") is None
    finally:
        await registry.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_registry_has_stable_errors_and_static_precedence(
    backend: Literal["memory", "sqlite"],
    tmp_path: Path,
) -> None:
    static = _target("reserved")
    registry = await _registry(backend, tmp_path, static)
    try:
        with pytest.raises(GraphicalTargetAlreadyExistsError, match="graphical target already exists"):
            await registry.create(SYSTEM, static)
        with pytest.raises(GraphicalTargetImmutableError, match="static graphical target is immutable"):
            await registry.update(SYSTEM, static)
        with pytest.raises(GraphicalTargetImmutableError, match="static graphical target is immutable"):
            await registry.delete(SYSTEM, "reserved")
        with pytest.raises(GraphicalTargetNotFoundError, match="graphical target not found"):
            await registry.update(SYSTEM, _target("missing"))
        with pytest.raises(GraphicalTargetNotFoundError, match="graphical target not found"):
            await registry.delete(SYSTEM, "missing")
        await registry.create(SYSTEM, _target("duplicate"))
        with pytest.raises(GraphicalTargetAlreadyExistsError, match="graphical target already exists"):
            await registry.create(SYSTEM, _target("duplicate"))

        # A legacy runtime collision remains hidden and cannot produce duplicates.
        await registry._run_tx(lambda store: store.put_graphical_target(registry._to_record(static)))
        assert await registry.get(SYSTEM, "reserved") == static
        assert [target.target_id for target in await registry.list(SYSTEM)].count("reserved") == 1
    finally:
        await registry.close()


async def test_registry_persists_references_without_resolving_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHICAL_CA", "super-secret-material")
    registry = await _registry("sqlite", tmp_path)
    try:
        await registry.create(SYSTEM, _target("safe"))
        record = await registry.get_runtime_record(SYSTEM, "safe")
        assert record is not None
        assert record.ca_secret_ref == "env:GRAPHICAL_CA"  # pragma: allowlist secret
        assert "super-secret-material" not in repr(record)
    finally:
        await registry.close()


async def test_registry_rolls_back_operation_errors_and_retries_commit_conflicts(tmp_path: Path) -> None:
    registry = await _registry("memory", tmp_path)
    try:
        first_tx = AsyncMock()
        first_tx.commit.side_effect = ControlPlaneConflictError("conflict")
        second_tx = AsyncMock()
        operation_error_tx = AsyncMock()
        store = AsyncMock()
        store.get_graphical_target.return_value = None
        plane = MagicMock()
        plane.begin = AsyncMock(side_effect=[first_tx, second_tx, operation_error_tx])
        plane.close = AsyncMock()
        plane.graphical_target_store.return_value = store
        registry._control_plane = cast("Any", plane)

        assert await registry.create(SYSTEM, _target("retried")) == _target("retried")
        first_tx.rollback.assert_awaited_once()
        second_tx.commit.assert_awaited_once()

        async def fail(_store: object) -> None:
            raise RuntimeError("operation failed")

        with pytest.raises(RuntimeError, match="operation failed"):
            await registry._run_tx(fail)
        operation_error_tx.rollback.assert_awaited_once()
    finally:
        await registry.close()


async def test_registry_exhausted_conflicts_raise_stable_error(tmp_path: Path) -> None:
    registry = await _registry("memory", tmp_path)
    try:
        transactions = [AsyncMock() for _ in range(3)]
        for transaction in transactions:
            transaction.commit.side_effect = ControlPlaneConflictError("sensitive backend detail")
        store = AsyncMock()
        store.get_graphical_target.return_value = None
        plane = MagicMock()
        plane.begin = AsyncMock(side_effect=transactions)
        plane.close = AsyncMock()
        plane.graphical_target_store.return_value = store
        registry._control_plane = cast("Any", plane)

        with pytest.raises(GraphicalTargetTransactionError, match="graphical target transaction conflicted") as exc:
            await registry.create(SYSTEM, _target("conflict"))
        assert "sensitive backend detail" not in str(exc.value)
        assert exc.value.__cause__ is None
        assert "sensitive backend detail" not in "".join(traceback.format_exception(exc.value))
        assert all(transaction.rollback.await_count == 1 for transaction in transactions)
    finally:
        await registry.close()


async def test_registry_close_is_idempotent_and_respects_ownership() -> None:
    plane = AsyncMock()
    borrowed = GraphicalTargetRegistry((), plane)
    await borrowed.close()
    await borrowed.close()
    plane.close.assert_not_awaited()

    owned = GraphicalTargetRegistry((), plane, owns_control_plane=True)
    await owned.close()
    await owned.close()
    plane.close.assert_awaited_once()


async def test_registry_tenant_scope_restricts_all_reads_and_mutations(tmp_path: Path) -> None:
    static = _target("static-b", tenant_id="two")
    registry = await _registry("memory", tmp_path, static)
    try:
        await registry.create(TENANT_TWO, _target("runtime-b", tenant_id="two"))
        await registry.create(SYSTEM, _target("system-only"))
        assert await registry.get(TENANT_ONE, "static-b") is None
        assert await registry.get(TENANT_ONE, "runtime-b") is None
        assert await registry.get_runtime_record(TENANT_ONE, "runtime-b") is None
        assert await registry.get(TENANT_ONE, "system-only") is None
        with pytest.raises(GraphicalTargetForbiddenError, match="graphical target tenant scope denied"):
            await registry.create(TENANT_ONE, _target("new-b", tenant_id="two"))
        with pytest.raises(GraphicalTargetForbiddenError, match="graphical target tenant scope denied"):
            await registry.update(TENANT_ONE, _target("runtime-b", tenant_id="two"))
        with pytest.raises(GraphicalTargetForbiddenError, match="graphical target tenant scope denied"):
            await registry.delete(TENANT_ONE, "runtime-b")
        with pytest.raises(GraphicalTargetForbiddenError, match="graphical target tenant scope denied"):
            await registry.delete(TENANT_ONE, "system-only")
        assert await registry.get(SYSTEM, "static-b") == static
        assert await registry.get(SYSTEM, "runtime-b") == _target("runtime-b", tenant_id="two")
        await registry.delete(SYSTEM, "runtime-b")
        await registry.delete(SYSTEM, "system-only")
    finally:
        await registry.close()


async def test_registry_rolls_back_cancellation_and_store_factory_failure() -> None:
    transaction = AsyncMock()
    rolled_back = asyncio.Event()

    async def rollback() -> None:
        rolled_back.set()

    transaction.rollback.side_effect = rollback
    plane = MagicMock()
    plane.begin = AsyncMock(return_value=transaction)
    plane.graphical_target_store.return_value = object()
    registry = GraphicalTargetRegistry((), cast("Any", plane))

    async def wait_forever(_store: object) -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(registry._run_tx(wait_forever))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rolled_back.is_set()

    plane.graphical_target_store.side_effect = RuntimeError("store factory failed")
    with pytest.raises(RuntimeError, match="store factory failed"):
        await registry._run_tx(wait_forever)
    assert transaction.rollback.await_count == 2


async def test_registry_rolls_back_commit_cancellation() -> None:
    transaction = AsyncMock()
    transaction.commit.side_effect = asyncio.CancelledError
    plane = MagicMock()
    plane.begin = AsyncMock(return_value=transaction)
    plane.graphical_target_store.return_value = object()
    registry = GraphicalTargetRegistry((), cast("Any", plane))

    with pytest.raises(asyncio.CancelledError):
        await registry._run_tx(lambda _store: asyncio.sleep(0))
    transaction.rollback.assert_awaited_once()


async def test_registry_rollback_finishes_when_cleanup_is_cancelled() -> None:
    rollback_started = asyncio.Event()
    finish_rollback = asyncio.Event()
    transaction = AsyncMock()

    async def rollback() -> None:
        rollback_started.set()
        await finish_rollback.wait()

    transaction.rollback.side_effect = rollback
    plane = MagicMock()
    plane.begin = AsyncMock(return_value=transaction)
    plane.graphical_target_store.return_value = object()
    registry = GraphicalTargetRegistry((), cast("Any", plane))

    async def fail(_store: object) -> None:
        raise RuntimeError("primary failure")

    task = asyncio.create_task(registry._run_tx(fail))
    await rollback_started.wait()
    task.cancel()
    finish_rollback.set()
    with pytest.raises(RuntimeError, match="primary failure"):
        await task
    transaction.rollback.assert_awaited_once()


async def test_registry_preserves_primary_error_when_rollback_fails() -> None:
    transaction = AsyncMock()
    transaction.rollback.side_effect = RuntimeError("cleanup failure")
    plane = MagicMock()
    plane.begin = AsyncMock(return_value=transaction)
    plane.graphical_target_store.side_effect = ValueError("primary failure")
    registry = GraphicalTargetRegistry((), cast("Any", plane))

    with pytest.raises(ValueError, match="primary failure"):
        await registry._run_tx(lambda _store: asyncio.sleep(0))


async def test_registry_propagates_transaction_creation_failure_without_store_use() -> None:
    plane = MagicMock()
    plane.begin = AsyncMock(side_effect=RuntimeError("begin failed"))
    registry = GraphicalTargetRegistry((), cast("Any", plane))

    with pytest.raises(RuntimeError, match="begin failed"):
        await registry._run_tx(lambda _store: asyncio.sleep(0))
    plane.graphical_target_store.assert_not_called()


async def test_sqlite_cancellation_releases_transaction_connection(tmp_path: Path) -> None:
    plane = await bootstrap_control_plane(
        ControlPlaneConfig(backend="sqlite", database_url=str(tmp_path / "cancel.db")),
    )
    await plane.migrate()
    registry = GraphicalTargetRegistry((), plane)
    operation_started = asyncio.Event()

    async def wait_forever(_store: object) -> None:
        operation_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(registry._run_tx(wait_forever))
    await operation_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    transaction = await asyncio.wait_for(plane.begin(), timeout=1)
    await transaction.rollback()
    await registry.close()
    await plane.close()


async def test_registry_close_failure_can_retry_and_closed_operations_fail() -> None:
    plane = AsyncMock()
    plane.close.side_effect = [RuntimeError("close failed"), None]
    registry = GraphicalTargetRegistry((), plane, owns_control_plane=True)

    with pytest.raises(RuntimeError, match="close failed"):
        await registry.close()
    await registry.close()
    assert plane.close.await_count == 2
    with pytest.raises(GraphicalTargetClosedError, match="graphical target registry is closed"):
        await registry.list(SYSTEM)
