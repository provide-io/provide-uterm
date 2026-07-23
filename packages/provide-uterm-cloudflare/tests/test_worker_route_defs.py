"""RouteDef dispatch coverage for the Worker entrypoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
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


async def test_session_route_defs_proxy_connect_disconnect_and_events_watch() -> None:
    """Every session-scoped RouteDef is routed to the named Durable Object."""
    from provide.uterm.cloudflare.entry.handlers import _route_request

    stub = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(status=200, body="ok")))
    namespace = SimpleNamespace(idFromName=lambda session_id: f"do:{session_id}", get=lambda _id: stub)
    env = SimpleNamespace(SESSION_RUNTIME=namespace)

    for suffix in ("connect", "disconnect", "events/watch"):
        response = await _route_request(
            _request(f"/api/sessions/session-1/{suffix}", "POST" if suffix != "events/watch" else "GET"),
            env,
            _config(),
        )
        assert response.status == 200

    assert namespace.idFromName("session-1") == "do:session-1"
    assert stub.fetch.await_count == 3


async def test_route_def_wrong_method_returns_405_with_allow() -> None:
    from provide.uterm.cloudflare.entry.handlers import _route_request

    response = await _route_request(_request("/api/connect", "GET"), SimpleNamespace(), _config())

    assert response.status == 405
    assert response.headers["Allow"] == "POST"


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
