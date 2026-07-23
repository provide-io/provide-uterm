"""RouteDef-backed API dispatch for the Cloudflare Worker."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from provide.uterm.api_routes import API_ROUTE_REGISTRY, API_ROUTES, RouteDef, RouteScope
from provide.uterm.cloudflare.entry import auth as _auth_mod
from provide.uterm.cloudflare.entry.auth import _require_jwt
from provide.uterm.cloudflare.entry.fallback_stubs import CloudflareConfig, Response, json_response

GlobalHandler = Callable[[object, object, CloudflareConfig, Mapping[str, str]], Awaitable[Response]]


async def _sessions(request: object, env: object, config: CloudflareConfig, _params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_sessions

    return await _api_sessions(request, env, config)


async def _connect(request: object, env: object, config: CloudflareConfig, _params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_connect

    return await _api_connect(request, env, config)


async def _tunnels(request: object, env: object, config: CloudflareConfig, _params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_tunnels

    return await _api_tunnels(request, env, config)


async def _tunnel_revoke(request: object, env: object, config: CloudflareConfig, params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_tunnel_revoke

    return await _api_tunnel_revoke(request, env, config, params["tunnel_id"])


async def _tunnel_rotate(request: object, env: object, config: CloudflareConfig, params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_tunnel_rotate

    return await _api_tunnel_rotate(request, env, config, params["tunnel_id"])


async def _pam_events(request: object, env: object, config: CloudflareConfig, _params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_pam_events

    return await _api_pam_events(request, env, config)


async def _profiles(request: object, env: object, config: CloudflareConfig, _params: Mapping[str, str]) -> Response:
    from provide.uterm.cloudflare.entry.handlers import _api_profiles

    return await _api_profiles(request, env, config)


GLOBAL_CAPABILITIES: dict[str, GlobalHandler] = {
    "sessions.list": _sessions,
    "sessions.create": _sessions,
    "sessions.bulk_delete": _sessions,
    "tunnels.connect": _connect,
    "tunnels.create": _tunnels,
    "tunnels.revoke_token": _tunnel_revoke,
    "tunnels.rotate_token": _tunnel_rotate,
    "pam_events.ingest": _pam_events,
    "profiles.list": _profiles,
    "profiles.create": _profiles,
    "profiles.get": _profiles,
    "profiles.update": _profiles,
    "profiles.delete": _profiles,
    "profiles.connect": _profiles,
}


def _validate_global_capabilities() -> None:
    session_capabilities = {route.capability for route in API_ROUTES if route.scope is RouteScope.SESSION}
    if session_capabilities & set(GLOBAL_CAPABILITIES):
        msg = "session RouteDef capability registered in Worker"
        raise ValueError(msg)
    missing = sorted(
        {route.capability for route in API_ROUTES if route.scope is RouteScope.GLOBAL} - set(GLOBAL_CAPABILITIES)
    )
    if missing:
        msg = f"missing Worker route capabilities: {', '.join(missing)}"
        raise ValueError(msg)


_validate_global_capabilities()


def _matches_route_shape(path: str, route: RouteDef) -> bool:
    """Return whether *path* has a RouteDef's segments, ignoring parameter syntax."""
    path_segments = path.split("/")[1:]
    template_segments = route.template.split("/")[1:]
    if len(path_segments) != len(template_segments):
        return False
    return all(
        bool(actual) if expected.startswith("{") else actual == expected
        for actual, expected in zip(path_segments, template_segments, strict=True)
    )


async def _authorize_roles(route: RouteDef, request: object, config: CloudflareConfig) -> Response | None:
    """Enforce role alternatives after JWT authentication and before a capability."""
    if not route.roles:
        return None
    principal: Any = await _auth_mod._decode_jwt_principal(request, config)
    if principal is None and config.jwt.mode in {"dev", "none"}:  # ty:ignore[unresolved-attribute]
        return None
    roles = frozenset(getattr(principal, "roles", ()))
    if roles.isdisjoint(route.roles):
        return json_response({"error": "forbidden"}, status=403)
    return None


async def dispatch_api_route(request: object, env: object, config: CloudflareConfig, path: str) -> Response | None:
    """Dispatch a shared API RouteDef, returning ``None`` for non-API paths."""
    if not path.startswith("/api/"):
        return None
    method = str(getattr(request, "method", "GET")).upper()
    match = API_ROUTE_REGISTRY.match(method, path)
    if match is None:
        allowed = API_ROUTE_REGISTRY.allowed_methods(path)
        if allowed:
            return json_response(
                {"error": "method not allowed"},
                status=405,
                headers={"Allow": ", ".join(allowed_method.value for allowed_method in allowed)},
            )
        if any(_matches_route_shape(path, route) for route in API_ROUTES):
            return json_response({"error": "invalid route parameter"}, status=422)
        return json_response({"error": "not_found", "path": path}, status=404)

    auth_error = await _require_jwt(request, config)
    if auth_error is not None:
        return auth_error
    role_error = await _authorize_roles(match.route, request, config)
    if role_error is not None:
        return role_error
    if match.route.scope is RouteScope.SESSION:
        namespace = getattr(env, "SESSION_RUNTIME", None)
        if namespace is None:
            return json_response({"error": "SESSION_RUNTIME binding missing"}, status=500)
        response = await namespace.get(namespace.idFromName(match.params["session_id"])).fetch(request)
        if match.route.capability == "sessions.delete" and 200 <= response.status < 300:
            from provide.uterm.cloudflare.state.registry import delete_kv_session

            await delete_kv_session(env, match.params["session_id"])
        return response
    return await GLOBAL_CAPABILITIES[match.route.capability](request, env, config, match.params)


__all__ = ["GLOBAL_CAPABILITIES", "dispatch_api_route"]
