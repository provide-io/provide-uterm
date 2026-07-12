#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authenticated tenant-scoped runtime graphical-target REST API."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Query, Request
from pydantic import ValidationError

from provide.uterm.server.audit import audit_event
from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition
from provide.uterm.server.graphical import GraphicalTargetRegistry, GraphicalTargetScope
from provide.uterm.server.graphical.targets import (
    GraphicalTargetAlreadyExistsError,
    GraphicalTargetClosedError,
    GraphicalTargetForbiddenError,
    GraphicalTargetImmutableError,
    GraphicalTargetNotFoundError,
    GraphicalTargetTransactionError,
)
from provide.uterm.server.routes._helpers import authz, principal, source_ip

TargetId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


def _registry(request: Request) -> GraphicalTargetRegistry:
    value = getattr(request.app.state, "uterm_graphical_target_registry", None)
    if value is None:
        raise _error(503, "graphical_target_unavailable", "graphical target service is unavailable")
    return cast("GraphicalTargetRegistry", value)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _scope(request: Request, capability: str) -> tuple[GraphicalTargetScope, str]:
    actor = principal(request)
    if actor.tenant_id is None:
        raise _error(403, "tenant_required", "tenant identity is required")
    if not await authz(request).has_capability(actor, capability):
        raise _error(403, "capability_required", f"{capability} capability is required")
    return GraphicalTargetScope.tenant(actor.tenant_id), actor.subject_id


def _target(payload: dict[str, Any], tenant_id: str, *, target_id: str | None = None) -> GraphicalTargetDefinition:
    if "tenant_id" in payload:
        raise _error(422, "tenant_managed", "tenant_id is assigned from authenticated identity")
    values = dict(payload)
    if target_id is not None:
        supplied = values.get("target_id")
        if supplied is not None and supplied != target_id:
            raise _error(409, "target_id_mismatch", "target_id must match the request path")
        values["target_id"] = target_id
    values["tenant_id"] = tenant_id
    try:
        return GraphicalTargetDefinition.model_validate(values)
    except ValidationError as exc:
        raise _error(422, "graphical_target_invalid", "graphical target definition is invalid") from exc


def _public_target(target: GraphicalTargetDefinition) -> dict[str, Any]:
    return target.model_dump(
        mode="json",
        exclude={"ca_secret_ref", "client_cert_secret_ref", "client_key_secret_ref"},
    )


def _map_registry_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GraphicalTargetNotFoundError | GraphicalTargetForbiddenError):
        return _error(404, "graphical_target_not_found", "graphical target not found")
    if isinstance(exc, GraphicalTargetAlreadyExistsError):
        return _error(409, "graphical_target_exists", "graphical target already exists")
    if isinstance(exc, GraphicalTargetImmutableError):
        return _error(409, "graphical_target_immutable", "static graphical target is immutable")
    if isinstance(exc, GraphicalTargetTransactionError):
        return _error(409, "graphical_target_conflict", "graphical target transaction conflicted")
    if isinstance(exc, GraphicalTargetClosedError):
        return _error(503, "graphical_target_unavailable", "graphical target service is unavailable")
    return _error(503, "graphical_target_backend_error", "graphical target backend failed")


def create_graphical_targets_router() -> APIRouter:
    """Return CRUD routes that never expose the registry's system scope."""
    router = APIRouter(prefix="/graphical-targets", tags=["graphical-targets"])

    @router.get("")
    async def list_targets(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        scope, _actor = await _scope(request, "graphical.target.read")
        registry = _registry(request)
        try:
            targets = await registry.list(scope)
        except Exception as exc:
            raise _map_registry_error(exc) from exc
        return {
            "items": [_public_target(target) for target in targets[offset : offset + limit]],
            "limit": limit,
            "offset": offset,
            "total": len(targets),
        }

    @router.get("/{target_id}")
    async def get_target(request: Request, target_id: TargetId) -> dict[str, Any]:
        scope, _actor = await _scope(request, "graphical.target.read")
        registry = _registry(request)
        try:
            target = await registry.get(scope, target_id)
        except Exception as exc:
            raise _map_registry_error(exc) from exc
        if target is None:
            raise _error(404, "graphical_target_not_found", "graphical target not found")
        return _public_target(target)

    @router.post("", status_code=201)
    async def create_target(request: Request, payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        scope, actor = await _scope(request, "graphical.target.manage")
        target = _target(payload, cast("str", scope.tenant_id))
        registry = _registry(request)
        try:
            created = await registry.create(scope, target)
        except Exception as exc:
            raise _map_registry_error(exc) from exc
        audit_event(
            "graphical_target.create",
            principal=actor,
            source_ip=source_ip(request),
            detail={"target_id": created.target_id, "tenant_id": created.tenant_id},
        )
        return _public_target(created)

    @router.put("/{target_id}")
    async def update_target(
        request: Request, target_id: TargetId, payload: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        scope, actor = await _scope(request, "graphical.target.manage")
        target = _target(payload, cast("str", scope.tenant_id), target_id=target_id)
        registry = _registry(request)
        try:
            updated = await registry.update(scope, target)
        except Exception as exc:
            raise _map_registry_error(exc) from exc
        audit_event(
            "graphical_target.update",
            principal=actor,
            source_ip=source_ip(request),
            detail={"target_id": target_id, "tenant_id": target.tenant_id},
        )
        return _public_target(updated)

    @router.delete("/{target_id}", status_code=204)
    async def delete_target(request: Request, target_id: TargetId) -> None:
        scope, actor = await _scope(request, "graphical.target.manage")
        registry = _registry(request)
        try:
            await registry.delete(scope, target_id)
        except Exception as exc:
            raise _map_registry_error(exc) from exc
        audit_event(
            "graphical_target.delete",
            principal=actor,
            source_ip=source_ip(request),
            detail={"target_id": target_id, "tenant_id": scope.tenant_id},
        )

    return router
