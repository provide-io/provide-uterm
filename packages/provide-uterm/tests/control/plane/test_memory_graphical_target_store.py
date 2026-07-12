#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from typing import TYPE_CHECKING

import pytest

from provide.uterm.control.plane import ControlPlaneConfig, bootstrap_control_plane
from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord
from provide.uterm.control.plane.memory import MemoryControlPlane

if TYPE_CHECKING:
    from provide.uterm.control.plane.bootstrap import ControlPlane
    from provide.uterm.control.plane.sqlite.engine import SqliteControlPlane

    def _assert_bootstrap_backends_conform_without_casts(
        memory: MemoryControlPlane,
        sqlite: SqliteControlPlane,
    ) -> None:
        memory_plane: ControlPlane = memory
        sqlite_plane: ControlPlane = sqlite
        _ = (memory_plane, sqlite_plane)


def _record(target_id: str, *, endpoint: str | None = None, updated_at: float = 2.0) -> GraphicalTargetRecord:
    return GraphicalTargetRecord(
        target_id=target_id,
        endpoint=endpoint or f"dns:///{target_id}.example:443",
        tls_mode="mtls",
        ca_secret_ref="file:/run/secrets/graphical-ca.pem",  # pragma: allowlist secret
        client_cert_secret_ref="env:GRAPHICAL_CLIENT_CERT",  # pragma: allowlist secret
        client_key_secret_ref="file:/run/secrets/graphical-client.key",  # pragma: allowlist secret
        expected_server_name=f"{target_id}.example",
        allowed_vm_patterns=("prod-*", "shared-??"),
        tenant_id="tenant-1",
        minimum_role="operator",
        connect_timeout_s=5.0,
        handshake_timeout_s=10.0,
        read_timeout_s=30.0,
        write_timeout_s=15.0,
        shutdown_timeout_s=3.0,
        max_grpc_message_bytes=1_048_576,
        max_framebuffer_width=4096,
        max_framebuffer_height=2160,
        max_rectangles=1024,
        max_clipboard_bytes=65_536,
        max_pixel_allocation_bytes=35_389_440,
        allowed_cidrs=("203.0.113.0/24", "2001:db8::/32"),
        audit_labels=(("environment", "production"), ("owner", "compute")),
        created_at=1.0,
        updated_at=updated_at,
    )


def test_graphical_target_record_is_frozen_slotted_and_fixture_contains_no_resolved_secret_bytes() -> None:
    record = _record("target-a")

    with pytest.raises(FrozenInstanceError):
        record.endpoint = "dns:///attacker.example:443"  # type: ignore[misc]

    assert not hasattr(record, "__dict__")
    serialized = repr(asdict(record))
    assert "GRAPHICAL_CLIENT_CERT" in serialized
    assert "BEGIN CERTIFICATE" not in serialized
    assert "PRIVATE KEY" not in serialized


@pytest.mark.asyncio
async def test_memory_graphical_target_store_put_get_list_replace_and_delete() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    assert isinstance(plane, MemoryControlPlane)
    first = _record("target-b")
    second = _record("target-a")
    replacement = _record("target-b", endpoint="dns:///replacement.example:443", updated_at=3.0)

    tx = await plane.begin()
    store = plane.graphical_target_store(tx)
    await store.put_graphical_target(first)
    await store.put_graphical_target(second)
    await store.put_graphical_target(replacement)
    assert await store.get_graphical_target("target-b") == replacement
    assert await store.list_graphical_targets() == [second, replacement]
    await tx.commit()

    delete_tx = await plane.begin()
    delete_store = plane.graphical_target_store(delete_tx)
    assert await delete_store.delete_graphical_target("target-a") is True
    assert await delete_store.delete_graphical_target("missing") is False
    await delete_tx.commit()

    read_tx = await plane.begin()
    read_store = plane.graphical_target_store(read_tx)
    assert await read_store.get_graphical_target("target-a") is None
    assert await read_store.list_graphical_targets() == [replacement]
    await read_tx.rollback()


@pytest.mark.asyncio
async def test_memory_graphical_target_store_rollback_discards_changes() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    assert isinstance(plane, MemoryControlPlane)
    tx = await plane.begin()
    await plane.graphical_target_store(tx).put_graphical_target(_record("rolled-back"))
    await tx.rollback()

    read_tx = await plane.begin()
    assert await plane.graphical_target_store(read_tx).get_graphical_target("rolled-back") is None
    await read_tx.rollback()


@pytest.mark.asyncio
async def test_memory_graphical_target_store_detects_same_target_conflicts() -> None:
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    assert isinstance(plane, MemoryControlPlane)
    tx1 = await plane.begin()
    tx2 = await plane.begin()
    await plane.graphical_target_store(tx1).put_graphical_target(_record("shared", endpoint="dns:///one:443"))
    await plane.graphical_target_store(tx2).put_graphical_target(_record("shared", endpoint="dns:///two:443"))

    await tx1.commit()
    with pytest.raises(ControlPlaneConflictError):
        await tx2.commit()
