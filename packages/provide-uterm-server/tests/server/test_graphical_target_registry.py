#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Merged static and runtime graphical-target registry tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition
from provide.uterm.server.graphical import (
    GraphicalTargetAlreadyExistsError,
    GraphicalTargetImmutableError,
    GraphicalTargetNotFoundError,
    GraphicalTargetRegistry,
    GraphicalTargetTransactionError,
)
from provide.uterm.server.secrets import SecretReference


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
        created = await registry.create(_target("runtime", tenant_id="one"))

        assert created == _target("runtime", tenant_id="one")
        assert await registry.get("static") == static
        assert await registry.get("runtime") == created
        assert await registry.get("runtime", tenant_id="two") is None
        assert [target.target_id for target in await registry.list()] == ["runtime", "static"]
        assert [target.target_id for target in await registry.list(tenant_id="one")] == ["runtime", "static"]
        assert await registry.list(tenant_id="two") == []
        assert await registry.get("static", tenant_id="two") is None
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
        await registry.create(first)
        record_before = await registry.get_runtime_record("runtime")
        updated = _target("runtime", endpoint="dns:///changed.example:443")

        assert await registry.update(updated) == updated
        record_after = await registry.get_runtime_record("runtime")
        assert record_before is not None and record_after is not None
        assert record_after.created_at == record_before.created_at
        assert record_after.updated_at >= record_before.updated_at
        await registry.delete("runtime")
        assert await registry.get("runtime") is None
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
            await registry.create(static)
        with pytest.raises(GraphicalTargetImmutableError, match="static graphical target is immutable"):
            await registry.update(static)
        with pytest.raises(GraphicalTargetImmutableError, match="static graphical target is immutable"):
            await registry.delete("reserved")
        with pytest.raises(GraphicalTargetNotFoundError, match="graphical target not found"):
            await registry.update(_target("missing"))
        with pytest.raises(GraphicalTargetNotFoundError, match="graphical target not found"):
            await registry.delete("missing")
        await registry.create(_target("duplicate"))
        with pytest.raises(GraphicalTargetAlreadyExistsError, match="graphical target already exists"):
            await registry.create(_target("duplicate"))

        # A legacy runtime collision remains hidden and cannot produce duplicates.
        await registry._run_tx(lambda store: store.put_graphical_target(registry._to_record(static)))
        assert await registry.get("reserved") == static
        assert [target.target_id for target in await registry.list()].count("reserved") == 1
    finally:
        await registry.close()


async def test_registry_persists_references_without_resolving_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHICAL_CA", "super-secret-material")
    registry = await _registry("sqlite", tmp_path)
    try:
        await registry.create(_target("safe"))
        record = await registry.get_runtime_record("safe")
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

        assert await registry.create(_target("retried")) == _target("retried")
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
            await registry.create(_target("conflict"))
        assert "sensitive backend detail" not in str(exc.value)
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
