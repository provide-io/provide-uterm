"""HTTP API request handlers and the route dispatcher.

Exposes the per-route async handlers (``_handle_sessions``,
``_handle_connect``), the lightweight ``_api_*``
indirection used by the RouteDef capability map, and the top-level
``_route_request`` entry point used by ``Default.fetch``.
"""

from __future__ import annotations

import json as _json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from provide.uterm.cloudflare.entry import auth as _auth_mod
from provide.uterm.cloudflare.entry.auth import _require_jwt, _resolve_principal_id
from provide.uterm.cloudflare.entry.fallback_stubs import CloudflareConfig, Response, json_response
from provide.uterm.cloudflare.entry.share_tokens import _attach_share_token_cookie, _share_token_cookie_header
from provide.uterm.cloudflare.entry.spa import _resolve_spa_route, _spa_response

logger = logging.getLogger(__name__)


def _entry_attr(name: str) -> Any:
    """Resolve ``provide.uterm.cloudflare.entry.<name>`` lazily.

    Tests patch ``provide.uterm.cloudflare.entry.<name>`` (the package
    namespace) for the public re-exports ``get_kv_session``,
    ``list_kv_sessions``, ``delete_kv_session``, and ``serve_asset``.  Direct
    submodule imports here would capture the original references and bypass
    those patches; resolving through the package namespace at call time keeps
    the mocking surface intact for those public symbols.
    """
    from provide.uterm.cloudflare import entry as _entry_pkg

    return getattr(_entry_pkg, name)


async def _decode_jwt_principal(request: object, config: CloudflareConfig) -> Any:
    """Resolve the JWT principal via the ``auth`` submodule.

    Indirection via :mod:`provide.uterm.cloudflare.entry.auth` keeps tests
    able to patch ``entry.auth._decode_jwt_principal`` to inject fake
    principals.
    """
    return await _auth_mod._decode_jwt_principal(request, config)


_STATIC_ASSET_PATH = re.compile(r"^/[a-zA-Z0-9._/-]+\.(?:html|css|js)$")


async def _handle_sessions(request: object, env: object, config: CloudflareConfig) -> Response:
    """Handle GET/DELETE /api/sessions."""
    method = str(getattr(request, "method", "GET")).upper()
    if method == "DELETE":
        # Bulk delete is admin-only: any JWT principal must carry the admin role.
        principal = await _decode_jwt_principal(request, config)
        if principal is not None and "admin" not in principal.roles:
            return json_response({"error": "admin role required"}, status=403)
        kv = getattr(env, "SESSION_REGISTRY", None)
        if kv is None:
            return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)
        keys_resp = await kv.list(prefix="session:")
        keys = [k.name for k in keys_resp.keys]
        for key in keys:
            await kv.delete(key)
        return json_response({"ok": True, "deleted": len(keys)})
    # GET: mirror FastAPI's can_read_session policy so the fleet listing matches
    # what individual session reads would allow.
    # - admin/owner: see all sessions
    # - operator role: see public + operator-visibility + own sessions
    # - viewer/unauthenticated (None principal): see public sessions only
    principal = await _decode_jwt_principal(request, config)
    kv_configured = getattr(env, "SESSION_REGISTRY", None) is not None
    sessions = await _entry_attr("list_kv_sessions")(env)
    if principal is not None and "admin" not in principal.roles:
        is_operator = "operator" in principal.roles
        subject_id = principal.subject_id

        def _can_read(s: dict[str, object]) -> bool:
            if s.get("owner") == subject_id:
                return True
            vis = s.get("visibility", "public")
            if vis == "public":
                return True
            return bool(vis == "operator" and is_operator)

        sessions = [s for s in sessions if _can_read(s)]
    elif principal is None:
        # No JWT (open/dev mode) — return all sessions as-is.
        pass
    scope = "fleet" if kv_configured else "local"
    return json_response(sessions, headers={"X-Sessions-Scope": scope})


async def _handle_connect(request: object, env: object, config: CloudflareConfig) -> Response:
    """Handle POST /api/connect — create a session in KV."""
    method = str(getattr(request, "method", "GET")).upper()
    if method != "POST":
        return json_response({"error": "method not allowed"}, status=405)
    try:
        raw = await request.json()  # type: ignore[attr-defined]  # request is a Pyodide proxy  # ty:ignore[unresolved-attribute]
        body = raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception as exc:
        logger.debug("connect_body_parse_failed: %s", exc)
        body = {}
    connector_type = str(body.get("connector_type", "shell"))
    prefix = "ushell" if connector_type == "ushell" else "connect"
    session_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    display_name = str(body.get("display_name") or session_id)
    input_mode = str(body.get("input_mode", "open"))
    tags = list(body.get("tags") or [])
    created_at = time.time()
    principal = await _decode_jwt_principal(request, config)
    owner = principal.subject_id if principal is not None else None
    visibility = "private" if principal is not None else "public"
    entry = {
        "session_id": session_id,
        "display_name": display_name,
        "created_at": created_at,
        "connector_type": connector_type,
        "lifecycle_state": "waiting",
        "input_mode": input_mode,
        "connected": False,
        "auto_start": False,
        "tags": tags,
        "recording_enabled": True,
        "recording_available": False,
        "owner": owner,
        "visibility": visibility,
        "last_error": None,
    }
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)
    await kv.put(f"session:{session_id}", _json.dumps({**entry, "hijacked": False}))
    return json_response({**entry, "url": f"/app/session/{session_id}"})


async def _route_request(request: object, env: object, config: CloudflareConfig) -> Response:
    """Route an incoming request to the appropriate handler."""
    from provide.uterm.cloudflare.entry.registry import _extract_worker_id

    path = urlparse(str(request.url)).path  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]

    # Public routes (no auth).
    if path == "/api/health":
        return json_response({"ok": True, "service": "provide-uterm-cloudflare", "environment": config.environment})  # ty:ignore[unresolved-attribute]
    if path.startswith("/assets/"):
        return _entry_attr("serve_asset")(path.removeprefix("/assets/"))
    if _STATIC_ASSET_PATH.match(path):
        return _entry_attr("serve_asset")(path.removeprefix("/"))

    spa = _resolve_spa_route(path)
    # Share/SPA invite paths need tunnel helpers; load them only here so a
    # missing vendored ``provide.uterm.tunnel`` cannot 500 browser/worker WS
    # upgrades (those proxy to the DO without share resolution).
    if spa is not None and "session_id" in spa[1]:
        try:
            from provide.uterm.cloudflare.api._tunnel_api import (
                consume_tunnel_invite as _consume_tunnel_invite,
            )
            from provide.uterm.cloudflare.api._tunnel_api import (
                resolve_share_context as _resolve_share_context,
            )
        except ImportError:  # pragma: no cover
            import importlib

            _tunnel_api = importlib.import_module("api._tunnel_api")
            _consume_tunnel_invite = _tunnel_api.consume_tunnel_invite
            _resolve_share_context = _tunnel_api.resolve_share_context

        if spa[0] == "share":
            # /s/{id}?invite=... consumes a one-time invite, stamps an HttpOnly
            # cookie, then redirects to a clean /app URL.
            sid = str(spa[1]["session_id"])
            invite_context = await _consume_tunnel_invite(request, env, sid)
            if invite_context is not None:
                page, _share_role, token = invite_context
                target = f"/app/{page}/{sid}"
                headers = {"location": target}
                cookie = _share_token_cookie_header(request, sid, token)
                if cookie is not None:
                    headers["Set-Cookie"] = cookie
                return Response(None, status=302, headers=headers)  # ty:ignore[call-non-callable]
            share_context = await _resolve_share_context(request, env, sid, config)
            if share_context is None:
                return json_response({"error": "not_found", "session_id": sid}, status=404)
            page, _share_role = share_context
            target = f"/app/{page}/{sid}"
            return Response(None, status=302, headers={"location": target})  # ty:ignore[call-non-callable]

        share_context = await _resolve_share_context(request, env, str(spa[1]["session_id"]), config)
        if share_context is not None:
            _, share_role = share_context
            # Use the URL-requested page kind (inspect, replay, session, operator)
            # rather than the token-derived kind from resolve_share_context.
            page_kind = spa[0]
            response = _spa_response(
                page_kind,
                session_id=str(spa[1]["session_id"]),
                surface="operator" if share_role == "operator" else "user",
                share_role=share_role,
            )
            return _attach_share_token_cookie(response, request, str(spa[1]["session_id"]))

    if spa is not None:
        auth_error = await _require_jwt(request, config)
        if auth_error is not None:
            return auth_error
        return _spa_response(spa[0], **spa[1])

    from provide.uterm.cloudflare.entry.route_defs import dispatch_api_route

    api_response = await dispatch_api_route(request, env, config, path)
    if api_response is not None:
        return api_response

    # DO-proxied routes (includes /tunnel/{id} for WSS upgrade).
    worker_id = _extract_worker_id(path)
    if worker_id is not None:
        namespace = getattr(env, "SESSION_RUNTIME", None)
        if namespace is None:
            return json_response({"error": "SESSION_RUNTIME binding missing"}, status=500)
        return await namespace.get(namespace.idFromName(worker_id)).fetch(request)  # pragma: no cover

    return json_response({"error": "not_found", "path": path}, status=404)


async def _api_sessions(request: object, env: object, config: CloudflareConfig) -> Response:
    return await _handle_sessions(request, env, config)


async def _api_connect(request: object, env: object, config: CloudflareConfig) -> Response:
    return await _handle_connect(request, env, config)


async def _api_tunnels(request: object, env: object, config: CloudflareConfig) -> Response:
    try:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels
    except ImportError:  # pragma: no cover
        if TYPE_CHECKING:
            from provide.uterm.cloudflare.api._tunnel_api import handle_tunnels
        else:
            from api._tunnel_api import handle_tunnels

    principal = await _decode_jwt_principal(request, config)
    return await handle_tunnels(request, env, principal)


async def _api_tunnel_revoke(request: object, env: object, config: CloudflareConfig, tunnel_id: str) -> Response:
    try:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens
    except ImportError:  # pragma: no cover
        if TYPE_CHECKING:
            from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens
        else:
            from api._tunnel_api import handle_tunnel_revoke_tokens

    principal = await _decode_jwt_principal(request, config)
    return await handle_tunnel_revoke_tokens(request, env, tunnel_id, principal)


async def _api_tunnel_rotate(request: object, env: object, config: CloudflareConfig, tunnel_id: str) -> Response:
    try:
        from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens
    except ImportError:  # pragma: no cover
        if TYPE_CHECKING:
            from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens
        else:
            from api._tunnel_api import handle_tunnel_rotate_tokens

    principal = await _decode_jwt_principal(request, config)
    return await handle_tunnel_rotate_tokens(request, env, tunnel_id, principal, ttl_s=config.tunnel_token_ttl_s)  # ty:ignore[unresolved-attribute]


async def _api_pam_events(request: object, env: object, _config: CloudflareConfig) -> Response:
    try:
        from provide.uterm.cloudflare.api._pam import handle_pam_event
    except ImportError:  # pragma: no cover
        from api._pam import handle_pam_event  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]

    return await handle_pam_event(request, env)


async def _api_profiles(request: object, env: object, config: CloudflareConfig) -> Response:
    from provide.uterm.cloudflare.api._profiles import route_profiles

    path = urlparse(str(getattr(request, "url", ""))).path
    method = str(getattr(request, "method", "GET")).upper()
    # In dev mode, use a fixed principal. In JWT mode, resolve from token.
    principal_id = "dev-user" if config.jwt.mode == "dev" else await _resolve_principal_id(request, config)  # ty:ignore[unresolved-attribute]
    return await route_profiles(request, env, path, method, principal_id)


__all__ = [
    "_STATIC_ASSET_PATH",
    "_api_connect",
    "_api_pam_events",
    "_api_profiles",
    "_api_sessions",
    "_api_tunnel_revoke",
    "_api_tunnel_rotate",
    "_api_tunnels",
    "_handle_connect",
    "_handle_sessions",
    "_route_request",
]
