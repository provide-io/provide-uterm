#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI registration adapter for framework-neutral API route definitions."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

from provide.uterm.api_routes import API_ROUTE_REGISTRY, API_ROUTES, RouteDef

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from fastapi import APIRouter

    RoleAuthorizer = Callable[[Request, tuple[str, ...]], bool | Awaitable[bool]]


def bind_api_routes(
    router: APIRouter,
    capability_handlers: Mapping[str, Callable[..., object]],
    routes: Iterable[RouteDef],
    *,
    role_authorizer: RoleAuthorizer | None = None,
) -> None:
    """Register selected shared API routes with their capability handlers.

    The capability map is checked against the complete shared API inventory
    before mutating *router*, so incomplete adapters cannot leave it partly
    registered.
    """
    selected = tuple(routes)
    shared_routes = frozenset(API_ROUTES)
    if any(route not in shared_routes for route in selected):
        raise ValueError("route definition is not in API_ROUTES")

    API_ROUTE_REGISTRY.validate_capabilities(capability_handlers)
    if any(route.roles for route in selected) and role_authorizer is None:
        raise ValueError("role_authorizer is required for routes with required roles")

    for route in selected:
        router.add_api_route(
            route.template,
            capability_handlers[route.capability],
            methods=[route.method.value],
            name=route.operation,
            operation_id=route.operation,
            dependencies=[Depends(_route_guard(route, role_authorizer))],
        )


def _route_guard(route: RouteDef, role_authorizer: RoleAuthorizer | None) -> Callable[[Request], Awaitable[None]]:
    """Build the dependency that checks shared path grammar and route roles."""

    async def guard(request: Request) -> None:
        match = API_ROUTE_REGISTRY.match(route.method, request.url.path)
        if match is None or match.route != route:
            raise HTTPException(status_code=422, detail="invalid route path parameters")
        if route.roles and role_authorizer is not None:
            authorized = role_authorizer(request, route.roles)
            if inspect.isawaitable(authorized):
                authorized = await authorized
            if not authorized:
                raise HTTPException(status_code=403, detail="insufficient role privileges")

    return guard
