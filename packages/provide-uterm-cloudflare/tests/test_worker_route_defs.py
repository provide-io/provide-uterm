"""RouteDef dispatch coverage for the Worker entrypoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from provide.uterm.api_routes import API_ROUTES, RouteScope
from provide.uterm.cloudflare.config import CloudflareConfig


def _request(path: str, method: str = "GET") -> SimpleNamespace:
    return SimpleNamespace(
        url=f"https://example.invalid{path}",
        method=method,
        headers=SimpleNamespace(get=lambda _key, default=None: default),
    )


def _config() -> CloudflareConfig:
    config = CloudflareConfig()
    config.jwt.mode = "dev"
    return config


async def test_worker_proxies_every_session_route_def_to_the_named_durable_object() -> None:
    """Every documented session RouteDef is forwarded unchanged to its Durable Object."""
    from provide.uterm.cloudflare.entry.handlers import _route_request

    stub = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(status=200, body="ok")))
    namespace = SimpleNamespace(idFromName=lambda session_id: f"do:{session_id}", get=lambda _id: stub)
    env = SimpleNamespace(SESSION_RUNTIME=namespace)

    session_routes = tuple(route for route in API_ROUTES if route.scope is RouteScope.SESSION)
    for route in session_routes:
        response = await _route_request(
            _request(
                route.template.replace("{session_id}", "session-1").replace("{webhook_id}", "webhook-1"), route.method
            ),
            env,
            _config(),
        )
        assert response.status == 200

    assert namespace.idFromName("session-1") == "do:session-1"
    assert stub.fetch.await_count == len(session_routes)


class _RegistryKv:
    def __init__(self) -> None:
        self.values = {
            "session:session-1": json.dumps(
                {"session_id": "session-1", "worker_token_hash": "credential", "connected": True}
            )
        }

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


async def test_successful_do_session_delete_removes_fleet_registry_and_credentials() -> None:
    from provide.uterm.cloudflare.entry.route_defs import dispatch_api_route

    kv = _RegistryKv()
    stub = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(status=200, body='{"ok":true}')))
    namespace = SimpleNamespace(idFromName=lambda session_id: f"do:{session_id}", get=lambda _id: stub)
    env = SimpleNamespace(SESSION_RUNTIME=namespace, SESSION_REGISTRY=kv)

    response = await dispatch_api_route(
        _request("/api/sessions/session-1", "DELETE"), env, _config(), "/api/sessions/session-1"
    )

    assert response is not None
    assert response.status == 200
    assert "session:session-1" not in kv.values


async def test_failed_do_session_delete_keeps_fleet_registry_and_credentials() -> None:
    from provide.uterm.cloudflare.entry.route_defs import dispatch_api_route

    kv = _RegistryKv()
    stub = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(status=409, body='{"error":"failed"}')))
    namespace = SimpleNamespace(idFromName=lambda session_id: f"do:{session_id}", get=lambda _id: stub)
    env = SimpleNamespace(SESSION_RUNTIME=namespace, SESSION_REGISTRY=kv)

    response = await dispatch_api_route(
        _request("/api/sessions/session-1", "DELETE"), env, _config(), "/api/sessions/session-1"
    )

    assert response is not None
    assert response.status == 409
    assert json.loads(kv.values["session:session-1"])["worker_token_hash"] == "credential"


async def test_worker_dispatches_every_global_route_def_through_its_declared_capability() -> None:
    import provide.uterm.cloudflare.entry.route_defs as route_defs

    global_routes = tuple(route for route in API_ROUTES if route.scope is RouteScope.GLOBAL)
    handlers = {
        route.capability: AsyncMock(return_value=SimpleNamespace(status=200, body="ok")) for route in global_routes
    }

    with patch.dict(route_defs.GLOBAL_CAPABILITIES, handlers, clear=True):
        for route in global_routes:
            response = await route_defs.dispatch_api_route(
                _request(
                    route.template.replace("{tunnel_id}", "tunnel-1").replace("{profile_id}", "profile-1"), route.method
                ),
                SimpleNamespace(),
                _config(),
                route.template.replace("{tunnel_id}", "tunnel-1").replace("{profile_id}", "profile-1"),
            )
            assert response is not None
            assert response.status == 200

    assert {route.capability for route in global_routes} == set(handlers)
    assert all(handler.await_count == 1 for handler in handlers.values())


async def test_route_def_wrong_method_returns_405_with_allow() -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    response = await _route_request(_request("/api/connect", "GET"), SimpleNamespace(), _config())

    assert response.status == 405
    assert response.headers["Allow"] == "POST"


async def test_unknown_route_def_returns_404() -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    response = await _route_request(_request("/api/not-a-route"), SimpleNamespace(), _config())

    assert response.status == 404
    assert json.loads(response.body) == {"error": "not_found", "path": "/api/not-a-route"}


async def test_lifecycle_capabilities_publish_explicit_edge_refusals() -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    response = await _route_request(_request("/api/lifecycle/capabilities"), SimpleNamespace(), _config())

    assert response.status == 200
    assert json.loads(response.body) == {
        "browser_quota": {
            "supported": False,
            "error": "per_principal_browser_quota_unsupported",
            "refusal_route": "/api/lifecycle/browser-quota",
        },
        "governance": {
            "supported": False,
            "error": "unsupported_governance",
            "refusal_route": "/api/lifecycle/governance",
        },
    }


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/api/lifecycle/browser-quota", "per_principal_browser_quota_unsupported"),
        ("/api/lifecycle/governance", "unsupported_governance"),
    ],
)
async def test_lifecycle_unsupported_capability_routes_return_501(path: str, error: str) -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    response = await _route_request(_request(path), SimpleNamespace(), _config())

    assert response.status == 501
    assert json.loads(response.body) == {"error": error, "supported": False}


async def test_invalid_route_def_parameter_returns_422() -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    response = await _route_request(_request("/api/sessions/not%20valid"), SimpleNamespace(), _config())

    assert response.status == 422


async def test_pam_route_def_denies_viewer_before_capability() -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    viewer = SimpleNamespace(subject_id="viewer", roles=("viewer",))
    with patch("provide.uterm.cloudflare.entry.auth._decode_jwt_principal", new=AsyncMock(return_value=viewer)):
        response = await _route_request(_request("/api/pam-events", "POST"), SimpleNamespace(), _config())

    assert response.status == 403
    assert json.loads(response.body)["error"] == "forbidden"


@pytest.mark.parametrize(
    ("roles", "expected_status"),
    [((), 403), (("viewer",), 403), (("operator",), 200), (("admin",), 200)],
)
async def test_worker_pam_route_def_enforces_declared_roles(roles: tuple[str, ...], expected_status: int) -> None:
    import provide.uterm.cloudflare.entry.route_defs as route_defs

    pam_route = next(route for route in API_ROUTES if route.operation == "pam_events.ingest")
    handler = AsyncMock(return_value=SimpleNamespace(status=200, body="ok"))
    principal = SimpleNamespace(subject_id="test", roles=roles)
    with (
        patch.object(route_defs, "_require_jwt", new=AsyncMock(return_value=None)),
        patch("provide.uterm.cloudflare.entry.auth._decode_jwt_principal", new=AsyncMock(return_value=principal)),
        patch.dict(route_defs.GLOBAL_CAPABILITIES, {pam_route.capability: handler}),
    ):
        response = await route_defs.dispatch_api_route(
            _request(pam_route.template, pam_route.method), SimpleNamespace(), _config(), pam_route.template
        )

    assert response is not None
    assert response.status == expected_status
    assert handler.await_count == (expected_status == 200)


def test_worker_route_def_dispatch_replaces_legacy_matchers() -> None:
    import provide.uterm.cloudflare.entry.handlers as handlers
    import provide.uterm.cloudflare.entry.registry as registry

    for module, name in (
        (handlers, "_match_api_route"),
        (handlers, "_SESSION_ID_RE"),
        (handlers, "_TUNNEL_TOKENS_RE"),
        (handlers, "_TUNNEL_TOKENS_ROTATE_RE"),
        (registry, "_WORKER_ROUTE_PATTERNS"),
    ):
        assert not hasattr(module, name)


def test_worker_route_def_capability_validation_rejects_missing_global_handler() -> None:
    import provide.uterm.cloudflare.entry.route_defs as route_defs

    with patch.dict(route_defs.GLOBAL_CAPABILITIES, {}, clear=True):
        with pytest.raises(ValueError, match="missing Worker route capabilities"):
            route_defs._validate_global_capabilities()


def test_worker_route_def_capability_validation_rejects_session_handler_in_global_map() -> None:
    import provide.uterm.cloudflare.entry.route_defs as route_defs

    session_capability = next(route.capability for route in API_ROUTES if route.scope is RouteScope.SESSION)
    with patch.dict(route_defs.GLOBAL_CAPABILITIES, {session_capability: AsyncMock()}):
        with pytest.raises(ValueError, match="session RouteDef capability registered in Worker"):
            route_defs._validate_global_capabilities()
