#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI registration adapter for framework-neutral API route definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from provide.uterm.api_routes import API_ROUTE_REGISTRY, API_ROUTES, RouteDef

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from fastapi import APIRouter


def register_route_defs(
    router: APIRouter,
    capability_handlers: Mapping[str, Callable[..., object]],
    routes: Iterable[RouteDef],
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
    for route in selected:
        router.add_api_route(
            route.template,
            capability_handlers[route.capability],
            methods=[route.method.value],
            name=route.operation,
        )
