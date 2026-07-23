#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the RouteDef-to-FastAPI registration adapter."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from provide.uterm.api_routes import API_ROUTES, HttpMethod, RouteDef, RouteScope
from provide.uterm.server.routes.api import create_api_router
from provide.uterm.server.routes.route_defs import bind_api_routes

_EXPECTED_SESSION_ROUTE_CONTRACT = frozenset(
    {
        ("sessions.list", "sessions.list"),
        ("sessions.create", "sessions.create"),
        ("sessions.bulk_delete", "sessions.bulk_delete"),
        ("sessions.get", "sessions.get"),
        ("sessions.update", "sessions.update"),
        ("sessions.delete", "sessions.delete"),
        ("sessions.connect", "sessions.connect"),
        ("sessions.disconnect", "sessions.disconnect"),
        ("sessions.restart", "sessions.restart"),
        ("sessions.set_mode", "sessions.set_mode"),
        ("sessions.clear", "sessions.clear"),
        ("sessions.annotate", "sessions.annotate"),
        ("sessions.analyze", "sessions.analyze"),
        ("sessions.snapshot", "sessions.snapshot"),
        ("sessions.events", "sessions.events"),
        ("sessions.events_watch", "sessions.events_watch"),
        ("sessions.recording", "sessions.recording"),
        ("sessions.recording_entries", "sessions.recording_entries"),
        ("sessions.recording_download", "sessions.recording_download"),
    }
)


async def _handler() -> dict[str, bool]:
    return {"ok": True}


def _capability_handlers() -> dict[str, Callable[..., object]]:
    return {route.capability: _handler for route in API_ROUTES}


def test_registers_selected_route_defs_with_contract_metadata_and_fastapi_405() -> None:
    router = APIRouter()
    selected = tuple(route for route in API_ROUTES if route.operation in {"sessions.list", "sessions.create"})

    bind_api_routes(router, _capability_handlers(), selected)

    registered = {(route.path, frozenset(route.methods or ()), route.name) for route in router.routes}
    assert registered == {
        ("/api/sessions", frozenset({"GET"}), "sessions.list"),
        ("/api/sessions", frozenset({"POST"}), "sessions.create"),
    }

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.put("/api/sessions").status_code == 405
    assert app.openapi()["paths"]["/api/sessions"]["get"]["operationId"] == "sessions.list"


def test_rejects_missing_registry_capability_before_registering_any_selected_route() -> None:
    router = APIRouter()
    handlers = _capability_handlers()
    handlers.pop("profiles.connect")
    selected = (next(route for route in API_ROUTES if route.operation == "sessions.list"),)

    with pytest.raises(ValueError, match="missing route capabilities: profiles.connect"):
        bind_api_routes(router, handlers, selected)

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
        bind_api_routes(router, _capability_handlers() | {"metrics.read": _handler}, (fastapi_only,))

    assert router.routes == []


@pytest.mark.parametrize("session_id", ["bad.dot", "a" * 65])
def test_rejects_path_parameters_outside_the_shared_route_grammar(session_id: str) -> None:
    router = APIRouter()
    selected = (next(route for route in API_ROUTES if route.operation == "sessions.get"),)
    bind_api_routes(router, _capability_handlers(), selected)

    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get(f"/api/sessions/{session_id}").status_code == 422


def test_requires_a_role_authorizer_before_registering_role_protected_route_defs() -> None:
    router = APIRouter()
    selected = (next(route for route in API_ROUTES if route.operation == "sessions.bulk_delete"),)

    with pytest.raises(ValueError, match="role_authorizer"):
        bind_api_routes(router, _capability_handlers(), selected)

    assert router.routes == []


def test_rejects_unauthorized_role_protected_route_before_handler_execution() -> None:
    called = False

    async def protected_handler() -> dict[str, bool]:
        nonlocal called
        called = True
        return {"ok": True}

    def deny_roles(request: Request, required_roles: tuple[str, ...]) -> bool:
        assert request.url.path == "/api/sessions"
        assert required_roles == ("admin",)
        return False

    router = APIRouter()
    selected = (next(route for route in API_ROUTES if route.operation == "sessions.bulk_delete"),)
    bind_api_routes(
        router,
        _capability_handlers() | {"sessions.bulk_delete": protected_handler},
        selected,
        role_authorizer=deny_roles,
    )

    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).request("DELETE", "/api/sessions", json={}).status_code == 403
    assert not called


def test_api_router_binds_shared_session_route_defs_once() -> None:
    from provide.uterm.server.routes.sessions import session_capability_handlers

    expected_operations = {operation for operation, _ in _EXPECTED_SESSION_ROUTE_CONTRACT}
    expected_capabilities = {capability for _, capability in _EXPECTED_SESSION_ROUTE_CONTRACT}
    assert set(session_capability_handlers()) == expected_capabilities

    router = create_api_router()
    session_routes = [
        route for route in router.routes if isinstance(route, APIRoute) and route.path.startswith("/api/sessions")
    ]
    registered_operations = {
        route.operation_id
        for route in session_routes
        if route.operation_id is not None and route.operation_id.startswith("sessions.")
    }
    assert registered_operations == expected_operations
    assert sum(route.path == "/api/sessions" and route.methods == {"GET"} for route in session_routes) == 1
    assert sum(route.path == "/api/sessions" and route.methods == {"POST"} for route in session_routes) == 1
    assert sum(route.path == "/api/sessions" and route.methods == {"DELETE"} for route in session_routes) == 1


async def test_bulk_delete_role_authorizer_uses_existing_admin_policy() -> None:
    authorization = MagicMock()
    authorization.is_admin = AsyncMock(return_value=False)
    app = FastAPI()
    app.state.uterm_authz = authorization

    @app.middleware("http")
    async def set_principal(request: Request, call_next: Callable[..., object]) -> object:
        request.state.uterm_principal = SimpleNamespace(subject_id="viewer", roles=frozenset({"viewer"}))
        return await call_next(request)

    app.include_router(create_api_router())
    response = TestClient(app).request("DELETE", "/api/sessions", json={"filter": {}})

    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient role privileges"
    authorization.is_admin.assert_awaited_once()
