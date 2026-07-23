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
from fastapi import APIRouter, FastAPI, HTTPException, Request
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

_EXPECTED_PROFILE_ROUTE_CONTRACT = frozenset(
    {
        ("profiles.list", "profiles.list", "/api/profiles", frozenset({"GET"})),
        ("profiles.create", "profiles.create", "/api/profiles", frozenset({"POST"})),
        ("profiles.get", "profiles.get", "/api/profiles/{profile_id}", frozenset({"GET"})),
        ("profiles.update", "profiles.update", "/api/profiles/{profile_id}", frozenset({"PUT"})),
        ("profiles.delete", "profiles.delete", "/api/profiles/{profile_id}", frozenset({"DELETE"})),
        (
            "profiles.connect",
            "profiles.connect",
            "/api/profiles/{profile_id}/connect",
            frozenset({"POST"}),
        ),
    }
)

_EXPECTED_TUNNEL_ROUTE_CONTRACT = frozenset(
    {
        ("tunnels.connect", "tunnels.connect", "/api/connect", frozenset({"POST"})),
        ("tunnels.create", "tunnels.create", "/api/tunnels", frozenset({"POST"})),
        (
            "tunnels.revoke_token",
            "tunnels.revoke_token",
            "/api/tunnels/{tunnel_id}/tokens",
            frozenset({"DELETE"}),
        ),
        (
            "tunnels.rotate_token",
            "tunnels.rotate_token",
            "/api/tunnels/{tunnel_id}/tokens/rotate",
            frozenset({"POST"}),
        ),
    }
)

_EXPECTED_WEBHOOK_SSE_ROUTE_CONTRACT = frozenset(
    {
        (
            "sessions.events_stream",
            "sessions.events_stream",
            "/api/sessions/{session_id}/events/stream",
            frozenset({"GET"}),
        ),
        (
            "sessions.webhooks.create",
            "sessions.webhooks.create",
            "/api/sessions/{session_id}/webhooks",
            frozenset({"POST"}),
        ),
        (
            "sessions.webhooks.list",
            "sessions.webhooks.list",
            "/api/sessions/{session_id}/webhooks",
            frozenset({"GET"}),
        ),
        (
            "sessions.webhooks.delete",
            "sessions.webhooks.delete",
            "/api/sessions/{session_id}/webhooks/{webhook_id}",
            frozenset({"DELETE"}),
        ),
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
        route.operation_id for route in session_routes if route.operation_id in expected_operations
    }
    assert registered_operations == expected_operations
    assert sum(route.path == "/api/sessions" and route.methods == {"GET"} for route in session_routes) == 1
    assert sum(route.path == "/api/sessions" and route.methods == {"POST"} for route in session_routes) == 1
    assert sum(route.path == "/api/sessions" and route.methods == {"DELETE"} for route in session_routes) == 1


def test_api_router_binds_shared_profile_route_defs_once() -> None:
    from provide.uterm.server.routes.profiles import profile_capability_handlers

    expected_capabilities = {capability for _, capability, _, _ in _EXPECTED_PROFILE_ROUTE_CONTRACT}
    assert set(profile_capability_handlers()) == expected_capabilities

    router = create_api_router()
    profile_routes = [
        route for route in router.routes if isinstance(route, APIRoute) and route.path.startswith("/api/profiles")
    ]
    registered = {
        (route.operation_id, route.name, route.path, frozenset(route.methods or ())) for route in profile_routes
    }
    assert registered == _EXPECTED_PROFILE_ROUTE_CONTRACT
    for _, _, path, methods in _EXPECTED_PROFILE_ROUTE_CONTRACT:
        assert sum(route.path == path and route.methods == set(methods) for route in profile_routes) == 1


def test_api_router_binds_shared_tunnel_route_defs_once() -> None:
    from provide.uterm.server.routes.tunnels import tunnel_capability_handlers

    expected_capabilities = {capability for _, capability, _, _ in _EXPECTED_TUNNEL_ROUTE_CONTRACT}
    assert set(tunnel_capability_handlers()) == expected_capabilities

    router = create_api_router()
    tunnel_routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and (route.path == "/api/connect" or route.path.startswith("/api/tunnels"))
    ]
    registered = {
        (route.operation_id, route.name, route.path, frozenset(route.methods or ())) for route in tunnel_routes
    }
    assert registered == _EXPECTED_TUNNEL_ROUTE_CONTRACT
    for _, _, path, methods in _EXPECTED_TUNNEL_ROUTE_CONTRACT:
        assert sum(route.path == path and route.methods == set(methods) for route in tunnel_routes) == 1


def test_api_router_binds_shared_webhook_and_sse_route_defs_once() -> None:
    from provide.uterm.server.routes.sse import sse_capability_handlers
    from provide.uterm.server.routes.webhooks import webhook_capability_handlers

    expected_capabilities = {capability for _, capability, _, _ in _EXPECTED_WEBHOOK_SSE_ROUTE_CONTRACT}
    assert set(sse_capability_handlers()) | set(webhook_capability_handlers()) == expected_capabilities

    router = create_api_router()
    routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and (route.path.endswith("/events/stream") or "/webhooks" in route.path)
    ]
    registered = {(route.operation_id, route.name, route.path, frozenset(route.methods or ())) for route in routes}
    assert registered == _EXPECTED_WEBHOOK_SSE_ROUTE_CONTRACT
    for _, _, path, methods in _EXPECTED_WEBHOOK_SSE_ROUTE_CONTRACT:
        assert sum(route.path == path and route.methods == set(methods) for route in routes) == 1


def test_tunnel_route_adapter_rejects_invalid_tunnel_id() -> None:
    router = APIRouter()
    selected = (next(route for route in API_ROUTES if route.operation == "tunnels.revoke_token"),)
    bind_api_routes(router, _capability_handlers(), selected)

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).delete("/api/tunnels/bad.dot/tokens")

    assert response.status_code == 422


async def test_profile_capability_handlers_deny_viewer_create_and_connect() -> None:
    """Profile RouteDef handlers preserve the existing write authorization policy."""
    from provide.uterm.server.routes.profiles import profile_capability_handlers

    handlers = profile_capability_handlers()
    principal = SimpleNamespace(subject_id="viewer", roles=frozenset({"viewer"}))
    authorization = MagicMock()
    authorization.can_create_session = AsyncMock(return_value=False)
    authorization.can_read_profile = AsyncMock(return_value=True)
    profile = MagicMock()
    store = MagicMock()
    store.get_profile = AsyncMock(return_value=profile)
    request = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=principal),
        app=SimpleNamespace(
            state=SimpleNamespace(uterm_authz=authorization, uterm_profile_store=store, uterm_registry=MagicMock())
        ),
    )

    with pytest.raises(HTTPException, match="insufficient privileges") as create_error:
        await handlers["profiles.create"](request, {})
    assert create_error.value.status_code == 403

    with pytest.raises(HTTPException, match="insufficient privileges") as connect_error:
        await handlers["profiles.connect"](request, "shared-profile", {})
    assert connect_error.value.status_code == 403
    authorization.can_read_profile.assert_awaited_once_with(principal, profile)
    assert authorization.can_create_session.await_count == 2


@pytest.mark.parametrize("capability", ["tunnels.connect", "tunnels.create"])
async def test_tunnel_capability_handlers_deny_viewer_creation(capability: str) -> None:
    """Tunnel creation RouteDef handlers preserve the existing write authorization policy."""
    from provide.uterm.server.routes.tunnels import tunnel_capability_handlers

    handlers = tunnel_capability_handlers()
    request = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=SimpleNamespace(subject_id="viewer", roles=frozenset({"viewer"}))),
        app=SimpleNamespace(state=SimpleNamespace(uterm_authz=MagicMock())),
    )
    request.app.state.uterm_authz.can_create_session = AsyncMock(return_value=False)

    with pytest.raises(HTTPException, match="insufficient privileges") as error:
        await handlers[capability](request, {})

    assert error.value.status_code == 403
    request.app.state.uterm_authz.can_create_session.assert_awaited_once_with(request.state.uterm_principal)


async def test_webhook_list_capability_keeps_control_authorization() -> None:
    """The GET webhook endpoint remains protected by session-control permission."""
    from provide.uterm.server.routes.webhooks import webhook_capability_handlers

    principal = SimpleNamespace(subject_id="viewer", roles=frozenset({"viewer"}))
    definition = SimpleNamespace(session_id="s1")
    authorization = MagicMock()
    authorization.can_mutate_session = AsyncMock(return_value=False)
    registry = MagicMock()
    registry.get_definition = AsyncMock(return_value=definition)
    request = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=principal),
        app=SimpleNamespace(
            state=SimpleNamespace(
                uterm_authz=authorization,
                uterm_registry=registry,
                uterm_webhooks=MagicMock(),
            )
        ),
    )

    with pytest.raises(HTTPException, match="insufficient privileges") as error:
        await webhook_capability_handlers()["sessions.webhooks.list"](request, "s1")

    assert error.value.status_code == 403
    authorization.can_mutate_session.assert_awaited_once_with(principal, definition, "session.control.update")
    request.app.state.uterm_webhooks.list_webhooks.assert_not_called()


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
