#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI registration adapter for framework-neutral API route definitions.

Mutation-enforced at killed==100 (100/104 killed, 4 documented equivalents in
mutation_equivalents.toml); bound suite:
tests/server/test_route_defs_mutation_killing.py.

This module measured **36.54%** until 2026-08-10 — the lowest in the perimeter,
because nothing tested it directly. Every RouteDef family exercises it, but only
implicitly, and implicit coverage executes code without discriminating anything.
It is worth the direct tests: ``_route_guard`` is the 422 path-grammar check and
the 403 role gate for the whole shared API surface at once.

Two mutants are easy to under-test and were caught only by measuring:

* Binding the guard with a null authorizer leaves the dependency attached and
  looks entirely normal from outside — the route still *has* a guard, it just
  never enforces roles. The suite pulls the dependency off the bound route and
  executes it.
* The catch-all loop walks ``{route.template for route in selected}`` — a SET,
  so ``continue`` vs ``break`` is observable only when a skipped single-method
  template happens to precede one that still needs its catch-all. The fixture
  picks a pair whose real iteration order exposes it rather than assuming one.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from provide.uterm.api_routes import API_ROUTE_REGISTRY, API_ROUTES, HttpMethod, RouteDef

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

    # Starlette's default 405 response is emitted by the first partial route
    # match, so a path with separate RouteDef handlers would otherwise expose
    # only that first method in ``Allow``.  A final catch-all keeps every
    # operation independently documented while preserving the registry's full
    # method contract for unsupported methods.
    for template in {route.template for route in selected}:
        selected_methods = tuple(route.method for route in selected if route.template == template)
        if len(selected_methods) < 2:
            continue
        allow = ", ".join(method.value for method in sorted(selected_methods, key=lambda method: method.value))

        async def method_not_allowed(_request: Request, *, allow: str = allow) -> JSONResponse:
            return JSONResponse({"detail": "Method Not Allowed"}, status_code=405, headers={"Allow": allow})

        router.add_api_route(
            template,
            method_not_allowed,
            methods=[method.value for method in HttpMethod],
            include_in_schema=False,
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
