#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Merged immutable-static and durable-runtime graphical target registry."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeVar

from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord
from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition

if TYPE_CHECKING:
    import builtins
    from collections.abc import AsyncIterator, Awaitable, Callable

    from provide.uterm.control.plane import ControlPlane
    from provide.uterm.control.plane.graphical_target import GraphicalTargetStore
    from provide.uterm.control.plane.transaction import Transaction

_T = TypeVar("_T")
_MAX_CONFLICT_ATTEMPTS = 3
_ROLLBACK_TIMEOUT_S = 1.0


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


class GraphicalTargetForbiddenError(GraphicalTargetError):
    """Raised when a tenant scope attempts a cross-tenant mutation."""


class GraphicalTargetClosedError(GraphicalTargetError):
    """Raised when an operation is attempted after registry shutdown."""


@dataclass(frozen=True, slots=True)
class GraphicalTargetScope:
    """Explicit tenant or privileged system scope for registry operations."""

    kind: Literal["tenant", "system"]
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if (self.kind == "tenant") != (self.tenant_id is not None):
            raise ValueError("tenant scope requires exactly one tenant_id")

    @classmethod
    def tenant(cls, tenant_id: str) -> GraphicalTargetScope:
        return cls("tenant", tenant_id)

    @classmethod
    def system(cls) -> GraphicalTargetScope:
        return cls("system")

    def permits(self, tenant_id: str | None) -> bool:
        return self.kind == "system" or self.tenant_id == tenant_id


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
        self._closing = False
        self._active_operations = 0
        self._lifecycle = asyncio.Condition()
        self._close_generation = 0
        self._close_outcomes: dict[int, BaseException | None] = {}

    async def get(self, scope: GraphicalTargetScope, target_id: str) -> GraphicalTargetDefinition | None:
        async with self._active_operation():
            return await self._get(scope, target_id)

    async def _get(self, scope: GraphicalTargetScope, target_id: str) -> GraphicalTargetDefinition | None:
        static = self._static.get(target_id)
        if static is not None:
            return static if scope.permits(static.tenant_id) else None
        record = await self._get_runtime_record(scope, target_id)
        if record is None:
            return None
        return self._from_record(record)

    async def list(self, scope: GraphicalTargetScope) -> list[GraphicalTargetDefinition]:
        async with self._active_operation():
            return await self._list_targets(scope)

    async def _list_targets(self, scope: GraphicalTargetScope) -> builtins.list[GraphicalTargetDefinition]:
        records = await self._run_tx_unleased(lambda store: store.list_graphical_targets())
        merged = {
            record.target_id: self._from_record(record) for record in records if record.target_id not in self._static
        }
        merged.update(self._static)
        return sorted(
            (target for target in merged.values() if scope.permits(target.tenant_id)),
            key=lambda target: target.target_id,
        )

    async def create(
        self,
        scope: GraphicalTargetScope,
        target: GraphicalTargetDefinition,
    ) -> GraphicalTargetDefinition:
        async with self._active_operation():
            return await self._create(scope, target)

    async def _create(
        self,
        scope: GraphicalTargetScope,
        target: GraphicalTargetDefinition,
    ) -> GraphicalTargetDefinition:
        self._require_scope(scope, target.tenant_id)
        if target.target_id in self._static:
            raise GraphicalTargetAlreadyExistsError("graphical target already exists")

        async def create_record(store: GraphicalTargetStore) -> None:
            if await store.get_graphical_target(target.target_id) is not None:
                raise GraphicalTargetAlreadyExistsError("graphical target already exists")
            await store.put_graphical_target(self._to_record(target))

        await self._run_tx_unleased(create_record)
        return target

    async def update(
        self,
        scope: GraphicalTargetScope,
        target: GraphicalTargetDefinition,
    ) -> GraphicalTargetDefinition:
        async with self._active_operation():
            return await self._update(scope, target)

    async def _update(
        self,
        scope: GraphicalTargetScope,
        target: GraphicalTargetDefinition,
    ) -> GraphicalTargetDefinition:
        self._require_scope(scope, target.tenant_id)
        if target.target_id in self._static:
            raise GraphicalTargetImmutableError("static graphical target is immutable")

        async def update_record(store: GraphicalTargetStore) -> None:
            current = await store.get_graphical_target(target.target_id)
            if current is None:
                raise GraphicalTargetNotFoundError("graphical target not found")
            self._require_scope(scope, current.tenant_id)
            await store.put_graphical_target(self._to_record(target, created_at=current.created_at))

        await self._run_tx_unleased(update_record)
        return target

    async def delete(self, scope: GraphicalTargetScope, target_id: str) -> None:
        async with self._active_operation():
            await self._delete(scope, target_id)

    async def _delete(self, scope: GraphicalTargetScope, target_id: str) -> None:
        static = self._static.get(target_id)
        if static is not None:
            self._require_scope(scope, static.tenant_id)
            raise GraphicalTargetImmutableError("static graphical target is immutable")

        async def delete_record(store: GraphicalTargetStore) -> None:
            current = await store.get_graphical_target(target_id)
            if current is None:
                raise GraphicalTargetNotFoundError("graphical target not found")
            self._require_scope(scope, current.tenant_id)
            await store.delete_graphical_target(target_id)

        await self._run_tx_unleased(delete_record)

    async def get_runtime_record(
        self,
        scope: GraphicalTargetScope,
        target_id: str,
    ) -> GraphicalTargetRecord | None:
        async with self._active_operation():
            return await self._get_runtime_record(scope, target_id)

    async def _get_runtime_record(
        self,
        scope: GraphicalTargetScope,
        target_id: str,
    ) -> GraphicalTargetRecord | None:
        record = await self._run_tx_unleased(lambda store: store.get_graphical_target(target_id))
        return record if record is None or scope.permits(record.tenant_id) else None

    async def close(self) -> None:
        async with self._lifecycle:
            if self._closed:
                return
            if self._closing:
                generation = self._close_generation
                await self._lifecycle.wait_for(lambda: generation in self._close_outcomes)
                outcome = self._close_outcomes[generation]
                if outcome is not None:
                    raise outcome
                return
            self._closing = True
            self._close_generation += 1
            generation = self._close_generation
        try:
            async with self._lifecycle:
                await self._lifecycle.wait_for(lambda: self._active_operations == 0)
            if self._owns_control_plane:
                await self._control_plane.close()
        except BaseException as exc:
            await self._publish_close_outcome_safely(generation, exc)
            raise
        cancellation = await self._publish_close_outcome_safely(generation, None)
        if cancellation is not None:
            raise cancellation

    async def _publish_close_outcome_safely(
        self,
        generation: int,
        outcome: BaseException | None,
    ) -> asyncio.CancelledError | None:
        publication = asyncio.create_task(self._publish_close_outcome(generation, outcome))
        cancellation: asyncio.CancelledError | None = None
        while not publication.done():
            try:
                await asyncio.shield(publication)
            except asyncio.CancelledError as exc:
                cancellation = exc
        publication.result()
        return cancellation

    async def _publish_close_outcome(self, generation: int, outcome: BaseException | None) -> None:
        async with self._lifecycle:
            self._closed = outcome is None
            self._closing = False
            self._close_outcomes[generation] = outcome
            self._lifecycle.notify_all()

    async def _run_tx(self, operation: Callable[[GraphicalTargetStore], Awaitable[_T]]) -> _T:
        async with self._active_operation():
            return await self._run_tx_unleased(operation)

    async def _run_tx_unleased(self, operation: Callable[[GraphicalTargetStore], Awaitable[_T]]) -> _T:
        for attempt in range(_MAX_CONFLICT_ATTEMPTS):
            transaction: Transaction | None = None
            try:
                transaction = await self._control_plane.begin()
                store = self._control_plane.graphical_target_store(transaction)
                result = await operation(store)
                await transaction.commit()
            except BaseException as exc:
                if transaction is not None:
                    await self._rollback(transaction)
                if not isinstance(exc, ControlPlaneConflictError):
                    raise
                if attempt + 1 == _MAX_CONFLICT_ATTEMPTS:
                    raise GraphicalTargetTransactionError("graphical target transaction conflicted") from None
                await asyncio.sleep(0)
                continue
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    async def _rollback(transaction: Transaction) -> None:
        rollback_task = asyncio.create_task(asyncio.wait_for(transaction.rollback(), timeout=_ROLLBACK_TIMEOUT_S))
        try:
            await asyncio.shield(rollback_task)
        except asyncio.CancelledError:
            with suppress(BaseException):
                await rollback_task
        except BaseException:
            pass

    @asynccontextmanager
    async def _active_operation(self) -> AsyncIterator[None]:
        async with self._lifecycle:
            if self._closing:
                raise GraphicalTargetClosedError("graphical target registry is closing")
            if self._closed:
                raise GraphicalTargetClosedError("graphical target registry is closed")
            self._active_operations += 1
        try:
            yield
        finally:
            async with self._lifecycle:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._lifecycle.notify_all()

    @staticmethod
    def _require_scope(scope: GraphicalTargetScope, tenant_id: str | None) -> None:
        if not scope.permits(tenant_id):
            raise GraphicalTargetForbiddenError("graphical target tenant scope denied")

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
