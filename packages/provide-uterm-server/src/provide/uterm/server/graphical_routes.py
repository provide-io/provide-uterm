#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST surface for graphical targets (``/api/graphical-targets``).

Python reference port of the C# canonical
(``packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.GraphicalTargets.cs``)
and the Go port (``packages/provide-uterm-go/server/routes_graphical.go``).

Access is gated on capability (``graphical.target.read`` /
``graphical.target.manage``) + tenant scope. The scope is derived from
``Principal.tenant_id`` (resolved from the authenticated identity), NEVER from
client input; a client-supplied ``tenant_id`` in a create/update body is
rejected 422 ``tenant_managed``. Every response body is a
:meth:`GraphicalTargetDefinition.public_copy` (secrets stripped).
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse

from provide.telemetry import get_logger
from provide.uterm.server.graphical_targets import (
    ERR_ALREADY_EXISTS,
    ERR_BACKEND,
    ERR_CONFLICT,
    ERR_IMMUTABLE,
    ERR_INVALID_PAYLOAD,
    ERR_NOT_FOUND,
    ERR_TARGET_ID_MISMATCH,
    ERR_TENANT_MANAGED,
    ERR_UNAVAILABLE,
    PAYLOAD_KEYS,
    PROTOCOL_RFB,
    SUPPORTED_PROTOCOLS,
    GraphicalTargetDefinition,
    GraphicalTargetError,
    GraphicalTargetErrorCode,
    InMemoryGraphicalTargetRegistry,
    parse_rfb_endpoint,
    scope_for_tenant,
)

if TYPE_CHECKING:
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.config_schema import GraphicalTargetConfig, UtermServerConfig
    from provide.uterm.server.graphical_targets import GraphicalTargetScope

logger = get_logger(__name__)

GRAPHICAL_TARGETS_PATH = "/api/graphical-targets"
CAP_READ = "graphical.target.read"
CAP_MANAGE = "graphical.target.manage"
MAX_GRAPHICAL_TARGET_PAGE = 200


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "uterm_principal", None)
    if principal is None:
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", principal)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def _registry(request: Request) -> InMemoryGraphicalTargetRegistry:
    return cast("InMemoryGraphicalTargetRegistry", request.app.state.uterm_graphical_targets)


def _graphical_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build the ``{"detail":{"code","message"}}`` envelope error."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _map_route_error(err: GraphicalTargetError) -> HTTPException:
    """Map a registry :class:`GraphicalTargetError` onto its HTTP status + code."""
    code = err.code
    if code is GraphicalTargetErrorCode.ALREADY_EXISTS:
        return _graphical_error(409, ERR_ALREADY_EXISTS, "graphical target already exists")
    if code is GraphicalTargetErrorCode.IMMUTABLE:
        return _graphical_error(409, ERR_IMMUTABLE, "static graphical target is immutable")
    if code is GraphicalTargetErrorCode.CONFLICT:
        return _graphical_error(409, ERR_CONFLICT, "graphical target transaction conflicted")
    if code is GraphicalTargetErrorCode.INVALID:
        return _graphical_error(422, ERR_INVALID_PAYLOAD, "graphical target definition is invalid")
    if code in (GraphicalTargetErrorCode.NOT_FOUND, GraphicalTargetErrorCode.FORBIDDEN):
        return _graphical_error(404, ERR_NOT_FOUND, "graphical target not found")
    if code is GraphicalTargetErrorCode.CLOSED:
        return _graphical_error(503, ERR_UNAVAILABLE, "graphical target service is unavailable")
    return _graphical_error(503, ERR_BACKEND, "graphical target backend failed")


async def _resolve_scope(request: Request, capability: str) -> tuple[GraphicalTargetScope, Principal]:
    """Require ``capability`` and derive the tenant scope from the principal.

    Both a missing capability and an absent/invalid tenant yield a flat
    ``403 {"detail": "graphical target access denied"}`` (mirrors ``DetailError``).
    """
    principal = _principal(request)
    if not await _authz(request).has_capability(principal, capability):
        raise HTTPException(status_code=403, detail="graphical target access denied")
    scope, ok = scope_for_tenant(principal.tenant_id or "")
    if not ok or scope is None:
        raise HTTPException(status_code=403, detail="graphical target access denied")
    return scope, principal


def _get_string(body: dict[str, Any], key: str, fallback: str | None) -> str | None:
    """Absent/null → fallback; wrong type → INVALID error (GetString)."""
    if key not in body:
        return fallback
    raw = body[key]
    if raw is None:
        return fallback
    if not isinstance(raw, str):
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, f"{key} must be a string")
    return raw


def _get_int(body: dict[str, Any], key: str, fallback: int) -> int:
    """Number or numeric string → int; else INVALID error (GetInt)."""
    if key not in body:
        return fallback
    raw = body[key]
    if raw is None:
        return fallback
    # bool is a subclass of int — reject it as a non-integer payload value.
    if isinstance(raw, bool):
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, f"{key} must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, f"{key} must be an integer") from None
    raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, f"{key} must be an integer")


def _parse_body(body: dict[str, Any]) -> tuple[GraphicalTargetDefinition, bool, bool]:
    """Read the typed fields; track whether target_id / tenant_id were present.

    Semantic validation (identifier, protocol, endpoint, dimensions) runs later
    in the registry's create/update (TryParseGraphicalTargetBody).
    """
    target = GraphicalTargetDefinition()
    target.display_name = _get_string(body, "display_name", "") or ""
    target.target_id = _get_string(body, "target_id", "") or ""
    target.protocol = _get_string(body, "protocol", PROTOCOL_RFB) or PROTOCOL_RFB
    target.endpoint = _get_string(body, "endpoint", None)
    target.secret = _get_string(body, "secret", None)
    target.ca_secret_ref = _get_string(body, "ca_secret_ref", None)
    target.client_cert_secret_ref = _get_string(body, "client_cert_secret_ref", None)
    target.client_key_secret_ref = _get_string(body, "client_key_secret_ref", None)
    target.width = _get_int(body, "width", 640)
    target.height = _get_int(body, "height", 480)
    target.tenant_id = ""

    has_tenant = "tenant_id" in body
    if has_tenant:
        raw_tenant = body["tenant_id"]
        if isinstance(raw_tenant, str):
            target.tenant_id = raw_tenant

    has_target_id = "target_id" in body
    return target, has_target_id, has_tenant


def _generate_target_id() -> str:
    return "gt-" + secrets.token_hex(6)


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Read a JSON object body; reject invalid JSON / non-object with 422."""
    try:
        raw = await request.json()
    except Exception:
        raise _graphical_error(422, ERR_INVALID_PAYLOAD, "invalid request body") from None
    if not isinstance(raw, dict):
        raise _graphical_error(422, ERR_INVALID_PAYLOAD, "invalid request body")
    for key in raw:
        if key not in PAYLOAD_KEYS:
            raise _graphical_error(422, ERR_INVALID_PAYLOAD, "invalid request body")
    return raw


def _public_json(target: GraphicalTargetDefinition, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=target.public_copy().to_wire_dict(), status_code=status_code)


def create_graphical_router() -> APIRouter:
    """Return a router mounting the ``/api/graphical-targets`` REST surface."""
    router = APIRouter()

    @router.get(GRAPHICAL_TARGETS_PATH)
    async def list_graphical_targets(request: Request) -> JSONResponse:
        scope, _ = await _resolve_scope(request, CAP_READ)
        limit = _parse_page_arg(request, "limit", 100, 1, MAX_GRAPHICAL_TARGET_PAGE, "limit must be between 1 and 200")
        offset = _parse_page_arg(request, "offset", 0, 0, None, "offset must be non-negative")
        try:
            rows = _registry(request).list(scope)
        except GraphicalTargetError as err:
            raise _map_route_error(err) from err
        total = len(rows)
        start = min(offset, total)
        end = min(start + limit, total)
        items = [row.public_copy().to_wire_dict() for row in rows[start:end]]
        return JSONResponse(content={"items": items, "limit": limit, "offset": offset, "total": total})

    @router.get(GRAPHICAL_TARGETS_PATH + "/{target_id}")
    async def get_graphical_target(request: Request, target_id: str = Path(...)) -> JSONResponse:
        scope, _ = await _resolve_scope(request, CAP_READ)
        try:
            target = _registry(request).get(scope, target_id)
        except GraphicalTargetError as err:
            raise _map_route_error(err) from err
        if target is None:
            raise _graphical_error(404, ERR_NOT_FOUND, "graphical target not found")
        return _public_json(target)

    @router.post(GRAPHICAL_TARGETS_PATH)
    async def create_graphical_target(request: Request) -> JSONResponse:
        scope, principal = await _resolve_scope(request, CAP_MANAGE)
        body = await _read_json_body(request)
        try:
            payload, has_target_id, has_tenant = _parse_body(body)
        except GraphicalTargetError as err:
            raise _graphical_error(422, ERR_INVALID_PAYLOAD, err.message) from err
        if has_tenant:
            raise _graphical_error(422, ERR_TENANT_MANAGED, "tenant_id is assigned from authenticated identity")
        if has_target_id:
            raise _graphical_error(422, ERR_INVALID_PAYLOAD, "target_id is server-assigned and cannot be supplied")

        payload.tenant_id = scope.tenant_id or ""
        payload.target_id = _generate_target_id()
        payload.is_system = False
        payload.created_by = principal.subject_id
        if not payload.display_name.strip():
            payload.display_name = "graphical-target"
        try:
            created = _registry(request).create(scope, payload)
        except GraphicalTargetError as err:
            raise _map_route_error(err) from err
        return _public_json(created, status_code=201)

    @router.put(GRAPHICAL_TARGETS_PATH + "/{target_id}")
    async def update_graphical_target(request: Request, target_id: str = Path(...)) -> JSONResponse:
        scope, principal = await _resolve_scope(request, CAP_MANAGE)
        body = await _read_json_body(request)
        try:
            payload, _has_target_id, has_tenant = _parse_body(body)
        except GraphicalTargetError as err:
            raise _graphical_error(422, ERR_INVALID_PAYLOAD, err.message) from err
        if has_tenant:
            raise _graphical_error(422, ERR_TENANT_MANAGED, "tenant_id is assigned from authenticated identity")
        if payload.target_id and payload.target_id != target_id:
            raise _graphical_error(409, ERR_TARGET_ID_MISMATCH, "target_id must match the request path")

        registry = _registry(request)
        try:
            existing = registry.get(scope, target_id)
        except GraphicalTargetError as err:
            raise _map_route_error(err) from err
        if existing is None:
            raise _graphical_error(404, ERR_NOT_FOUND, "graphical target not found")

        payload.target_id = target_id
        payload.tenant_id = existing.tenant_id
        payload.is_system = existing.is_system
        payload.updated_by = principal.subject_id
        if not payload.display_name.strip():
            payload.display_name = existing.display_name
        try:
            updated = registry.update(scope, payload)
        except GraphicalTargetError as err:
            raise _map_route_error(err) from err
        return _public_json(updated)

    @router.delete(GRAPHICAL_TARGETS_PATH + "/{target_id}")
    async def delete_graphical_target(request: Request, target_id: str = Path(...)) -> Response:
        scope, _ = await _resolve_scope(request, CAP_MANAGE)
        try:
            _registry(request).delete(scope, target_id)
        except GraphicalTargetError as err:
            raise _map_route_error(err) from err
        return Response(status_code=204)

    return router


def _parse_page_arg(request: Request, name: str, default: int, minimum: int, maximum: int | None, message: str) -> int:
    """Parse a pagination query arg with bounds; raise 422 on violation."""
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise _graphical_error(422, ERR_INVALID_PAYLOAD, message) from None
    if value < minimum or (maximum is not None and value > maximum):
        raise _graphical_error(422, ERR_INVALID_PAYLOAD, message)
    return value


def _config_to_definition(target: GraphicalTargetConfig) -> GraphicalTargetDefinition:
    """Port ToGraphicalTargetDefinition: config row → static definition."""
    protocol = (target.protocol or PROTOCOL_RFB).strip().lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise GraphicalTargetError(
            GraphicalTargetErrorCode.INVALID, f"unsupported graphical target protocol: {target.protocol}"
        )

    endpoint = target.target_address.strip()
    if protocol == PROTOCOL_RFB and not endpoint:
        raise GraphicalTargetError(
            GraphicalTargetErrorCode.INVALID,
            f"graphical target requires target_address for rfb protocol: {target.target_id}",
        )

    target_id = target.target_id.strip() or _generate_target_id()

    endpoint_value: str | None = None
    if protocol == PROTOCOL_RFB:
        host, port = parse_rfb_endpoint(endpoint)
        endpoint_value = f"{host}:{port}"

    display = target.name if target.name.strip() else target_id

    return GraphicalTargetDefinition(
        target_id=target_id,
        tenant_id=target.tenant_id.strip(),
        display_name=display,
        protocol=protocol,
        endpoint=endpoint_value,
        width=_clamp_dimension(target.width, 640),
        height=_clamp_dimension(target.height, 480),
        is_system=True,
        is_static=True,
    )


def _clamp_dimension(value: int, default: int) -> int:
    """<=0 → default, >8192 → 8192 (config-seed only; REST rejects out-of-range)."""
    if value <= 0:
        return default
    if value > 8192:
        return 8192
    return value


def seed_graphical_targets(config: UtermServerConfig) -> InMemoryGraphicalTargetRegistry:
    """Build a registry seeded with the enabled config targets (SeedGraphicalTargets)."""
    registry = InMemoryGraphicalTargetRegistry()
    for target in config.graphical_targets:
        if not target.enabled:
            continue
        registry.add_static(_config_to_definition(target))
    return registry
