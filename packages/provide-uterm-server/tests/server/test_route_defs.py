#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the RouteDef-to-FastAPI registration adapter."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from provide.uterm.api_routes import API_ROUTES, HttpMethod, RouteDef, RouteScope
from provide.uterm.server.routes.route_defs import register_route_defs


async def _handler() -> dict[str, bool]:
    return {"ok": True}


def _capability_handlers() -> dict[str, Callable[..., object]]:
    return {route.capability: _handler for route in API_ROUTES}


def test_registers_selected_route_defs_with_contract_metadata_and_fastapi_405() -> None:
    router = APIRouter()
    selected = tuple(route for route in API_ROUTES if route.operation in {"sessions.list", "sessions.create"})

    register_route_defs(router, _capability_handlers(), selected)

    registered = {(route.path, frozenset(route.methods or ()), route.name) for route in router.routes}
    assert registered == {
        ("/api/sessions", frozenset({"GET"}), "sessions.list"),
        ("/api/sessions", frozenset({"POST"}), "sessions.create"),
    }

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).put("/api/sessions")
    assert response.status_code == 405


def test_rejects_missing_registry_capability_before_registering_any_selected_route() -> None:
    router = APIRouter()
    handlers = _capability_handlers()
    handlers.pop("profiles.connect")
    selected = (next(route for route in API_ROUTES if route.operation == "sessions.list"),)

    with pytest.raises(ValueError, match="missing route capabilities: profiles.connect"):
        register_route_defs(router, handlers, selected)

    assert router.routes == []


def test_rejects_fastapi_only_route_defs_outside_the_shared_api_inventory() -> None:
    router = APIRouter()
    fastapi_only = RouteDef(
        "metrics.read",
        HttpMethod.GET,
        "/api/metrics",
        RouteScope.GLOBAL,
        "metrics.read",
        (),
    )

    with pytest.raises(ValueError, match="not in API_ROUTES"):
        register_route_defs(router, _capability_handlers() | {"metrics.read": _handler}, (fastapi_only,))

    assert router.routes == []
