#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Merged immutable-static and durable-runtime graphical target registry."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, TypeVar

from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord
from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from provide.uterm.control.plane import ControlPlane
    from provide.uterm.control.plane.graphical_target import GraphicalTargetStore

_T = TypeVar("_T")
_MAX_CONFLICT_ATTEMPTS = 3


class GraphicalTargetError(RuntimeError):
    """Base class for stable, non-sensitive graphical target failures."""


class GraphicalTargetAlreadyExistsError(GraphicalTargetError):
    """Raised when a runtime target ID is already reserved."""


class GraphicalTargetNotFoundError(GraphicalTargetError):
    """Raised when a runtime target does not exist."""


class GraphicalTargetImmutableError(GraphicalTargetError):
    """Raised when a caller attempts to mutate a static target."""


class GraphicalTargetTransactionError(GraphicalTargetError):
    """Raised when transaction conflict retries are exhausted."""


class GraphicalTargetRegistry:
    """Merge static target policy with runtime control-plane records.

    Static definitions always win collisions. The registry borrows its control
    plane unless ``owns_control_plane`` is explicitly set.
    """

    def __init__(
        self,
        static_targets: tuple[GraphicalTargetDefinition, ...],
        control_plane: ControlPlane,
        *,
        owns_control_plane: bool = False,
    ) -> None:
        self._static = {target.target_id: target for target in static_targets}
        self._control_plane = control_plane
        self._owns_control_plane = owns_control_plane
        self._closed = False

    async def get(self, target_id: str, *, tenant_id: str | None = None) -> GraphicalTargetDefinition | None:
        static = self._static.get(target_id)
        if static is not None:
            return static if static.tenant_id == tenant_id or tenant_id is None else None
        record = await self.get_runtime_record(target_id)
        if record is None or (tenant_id is not None and record.tenant_id != tenant_id):
            return None
        return self._from_record(record)

    async def list(self, *, tenant_id: str | None = None) -> list[GraphicalTargetDefinition]:
        records = await self._run_tx(lambda store: store.list_graphical_targets())
        merged = {
            record.target_id: self._from_record(record) for record in records if record.target_id not in self._static
        }
        merged.update(self._static)
        return sorted(
            (target for target in merged.values() if tenant_id is None or target.tenant_id == tenant_id),
            key=lambda target: target.target_id,
        )

    async def create(self, target: GraphicalTargetDefinition) -> GraphicalTargetDefinition:
        if target.target_id in self._static:
            raise GraphicalTargetAlreadyExistsError("graphical target already exists")

        async def create_record(store: GraphicalTargetStore) -> None:
            if await store.get_graphical_target(target.target_id) is not None:
                raise GraphicalTargetAlreadyExistsError("graphical target already exists")
            await store.put_graphical_target(self._to_record(target))

        await self._run_tx(create_record)
        return target

    async def update(self, target: GraphicalTargetDefinition) -> GraphicalTargetDefinition:
        if target.target_id in self._static:
            raise GraphicalTargetImmutableError("static graphical target is immutable")

        async def update_record(store: GraphicalTargetStore) -> None:
            current = await store.get_graphical_target(target.target_id)
            if current is None:
                raise GraphicalTargetNotFoundError("graphical target not found")
            await store.put_graphical_target(self._to_record(target, created_at=current.created_at))

        await self._run_tx(update_record)
        return target

    async def delete(self, target_id: str) -> None:
        if target_id in self._static:
            raise GraphicalTargetImmutableError("static graphical target is immutable")

        async def delete_record(store: GraphicalTargetStore) -> None:
            if await store.get_graphical_target(target_id) is None:
                raise GraphicalTargetNotFoundError("graphical target not found")
            await store.delete_graphical_target(target_id)

        await self._run_tx(delete_record)

    async def get_runtime_record(self, target_id: str) -> GraphicalTargetRecord | None:
        return await self._run_tx(lambda store: store.get_graphical_target(target_id))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_control_plane:
            await self._control_plane.close()

    async def _run_tx(self, operation: Callable[[GraphicalTargetStore], Awaitable[_T]]) -> _T:
        for attempt in range(_MAX_CONFLICT_ATTEMPTS):
            transaction = await self._control_plane.begin()
            store = self._control_plane.graphical_target_store(transaction)
            try:
                result = await operation(store)
                await transaction.commit()
            except ControlPlaneConflictError as exc:
                with suppress(Exception):
                    await transaction.rollback()
                if attempt + 1 == _MAX_CONFLICT_ATTEMPTS:
                    raise GraphicalTargetTransactionError("graphical target transaction conflicted") from exc
                continue
            except Exception:
                with suppress(Exception):
                    await transaction.rollback()
                raise
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _to_record(target: GraphicalTargetDefinition, *, created_at: float | None = None) -> GraphicalTargetRecord:
        now = time.time()
        return GraphicalTargetRecord(
            target_id=target.target_id,
            endpoint=target.endpoint,
            tls_mode=target.tls_mode,
            ca_secret_ref=None if target.ca_secret_ref is None else target.ca_secret_ref.value,
            client_cert_secret_ref=None
            if target.client_cert_secret_ref is None
            else target.client_cert_secret_ref.value,
            client_key_secret_ref=None if target.client_key_secret_ref is None else target.client_key_secret_ref.value,
            expected_server_name=target.expected_server_name,
            allowed_vm_patterns=target.allowed_vm_patterns,
            tenant_id=target.tenant_id,
            minimum_role=target.minimum_role,
            connect_timeout_s=target.connect_timeout_s,
            handshake_timeout_s=target.handshake_timeout_s,
            read_timeout_s=target.read_timeout_s,
            write_timeout_s=target.write_timeout_s,
            shutdown_timeout_s=target.shutdown_timeout_s,
            max_grpc_message_bytes=target.max_grpc_message_bytes,
            max_framebuffer_width=target.max_framebuffer_width,
            max_framebuffer_height=target.max_framebuffer_height,
            max_rectangles=target.max_rectangles,
            max_clipboard_bytes=target.max_clipboard_bytes,
            max_pixel_allocation_bytes=target.max_pixel_allocation_bytes,
            allowed_cidrs=target.allowed_cidrs,
            audit_labels=target.audit_labels,
            created_at=now if created_at is None else created_at,
            updated_at=now,
        )

    @staticmethod
    def _from_record(record: GraphicalTargetRecord) -> GraphicalTargetDefinition:
        return GraphicalTargetDefinition.model_validate(
            {
                field: getattr(record, field)
                for field in GraphicalTargetDefinition.model_fields
                if field not in {"created_at", "updated_at"}
            },
        )
