from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Import handler base classes directly from workers — these MUST resolve to the
# real CF runtime classes (not stubs) so Cloudflare's Pyodide validation phase
# detects Default/SessionRuntime as registered event handlers.
_DurableObject: type = object  # type: ignore[assignment]
try:
    from workers import DurableObject as _DurableObject  # type: ignore[import-not-found]  # pragma: no cover
    from workers import (  # pragma: no cover
        Response,  # type: ignore[import-not-found]
        WorkerEntrypoint,  # type: ignore[import-not-found]
    )
except ImportError:
    # Outside CF runtime (tests / local dev): stubs loaded below from cf_types.
    Response = None  # type: ignore[assignment]
    WorkerEntrypoint = None  # type: ignore[assignment]

# Ensure the current directory, its parent, and python_modules are in sys.path
# for Cloudflare runtime.  Pyodide loads modules from /session/metadata/ and
# needs explicit path configuration.
_current_file = Path(__file__).resolve()
_current_dir = str(_current_file.parent)  # .../provide.terminal.cloudflare/
_parent_dir = str(_current_file.parent.parent)  # contains provide.terminal.cloudflare/ as package
_python_modules_dir = str(_current_file.parent.parent.parent / "python_modules")

# In CF runtime, wrangler may flatten src/ so that entry.py is at /session/
# and the package is at /session/provide.terminal.cloudflare/.  Add /session/
# (the grandparent) as well as the typical /session/metadata/ parent.
_import_error: str | None = None

for _path in [_parent_dir, _current_dir, _python_modules_dir]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from provide.terminal.cloudflare.auth.jwt import (
        JwtValidationError,
        decode_jwt,
        extract_bearer_or_cookie,
    )
    from provide.terminal.cloudflare.cf_types import (  # type: ignore[assignment,no-redef]
        Response,
        WorkerEntrypoint,
        json_response,
    )
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.do.session_runtime import SessionRuntime
    from provide.terminal.cloudflare.state.registry import delete_kv_session, get_kv_session, list_kv_sessions
    from provide.terminal.cloudflare.ui.assets import read_asset_text, serve_asset
except ImportError:
    try:
        from auth.jwt import (  # type: ignore[import-not-found]
            JwtValidationError,
            decode_jwt,
            extract_bearer_or_cookie,
        )
        from cf_types import (  # type: ignore[assignment,no-redef,import-not-found]
            Response,
            WorkerEntrypoint,
            json_response,
        )
        from config import CloudflareConfig  # type: ignore[import-not-found]
        from do.session_runtime import SessionRuntime  # type: ignore[import-not-found]
        from state.registry import delete_kv_session, get_kv_session, list_kv_sessions  # type: ignore[import-not-found]
        from ui.assets import read_asset_text, serve_asset  # type: ignore[import-not-found]
    except Exception as _exc2:  # pragma: no cover — Pyodide validation phase only
        # Last resort for Pyodide validation phase — stubs for non-handler imports.
        # WorkerEntrypoint/Response/DurableObject are imported directly from workers
        # above, so handler registration always succeeds.
        import traceback as _tb

        _import_error = _tb.format_exc()
        JwtValidationError = Exception  # type: ignore[assignment]

        def decode_jwt(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def extract_bearer_or_cookie(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def json_response(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        CloudflareConfig = object  # type: ignore[assignment]

        class SessionRuntime(_DurableObject):  # type: ignore[assignment]
            """Stub DO for validation phase — real impl loaded at runtime."""

            async def fetch(self, _request):  # type: ignore[override]
                return Response.json({"error": "not initialized"}, status=503)  # type: ignore[union-attr]

        def delete_kv_session(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def list_kv_sessions(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def read_asset_text(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def serve_asset(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None


__all__ = ["Default", "ProvideTerminalCloudflareWorker", "SessionRuntime"]

_WORKER_ROUTE_PATTERNS = (
    re.compile(r"^/ws/browser/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/ws/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/ws/raw/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/tunnel/(?P<worker_id>[a-zA-Z0-9_-]{1,64})$"),
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/hijack(?:/.*)?$"),
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/(?:input_mode|disconnect_worker)$"),
    re.compile(
        r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})(?:/(?:snapshot|events|mode|clear|analyze|restart|recording(?:/(?:entries|download))?))?$"
    ),
    re.compile(r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/events/stream$"),
    re.compile(r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/webhooks(?:/[a-zA-Z0-9_-]{1,64})?$"),
)
_STATIC_ASSET_PATH = re.compile(r"^/[a-zA-Z0-9._/-]+\.(?:html|css|js)$")
_SESSION_ID_RE = re.compile(r"^/api/sessions/(?P<session_id>[a-zA-Z0-9_-]{1,64})$")
_SHARE_ROUTE_RE = re.compile(r"^/s/(?P<sid>[a-zA-Z0-9_-]{1,64})$")
_TUNNEL_TOKENS_RE = re.compile(r"^/api/tunnels/(?P<tunnel_id>[a-zA-Z0-9_-]{1,64})/tokens$")
_TUNNEL_TOKENS_ROTATE_RE = re.compile(r"^/api/tunnels/(?P<tunnel_id>[a-zA-Z0-9_-]{1,64})/tokens/rotate$")

_XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
_FITADDON_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
_FONTS_CDN = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"

# SPA route patterns → (page_kind, needs_session_id, extra_scripts).
_SPA_SESSION_RE = re.compile(r"^/app/(?P<kind>session|operator|replay|inspect)/(?P<sid>[a-zA-Z0-9_-]{1,64})$")


def _resolve_spa_route(path: str) -> tuple[str, dict[str, object]] | None:
    """Return (page_kind, extra_bootstrap) for SPA routes, or None."""
    if path in {"/", "/app", "/app/"}:
        return ("dashboard", {})
    if path in {"/app/connect", "/app/connect/"}:
        return ("connect", {})
    share_match = _SHARE_ROUTE_RE.match(path)
    if share_match:
        return ("share", {"session_id": share_match.group("sid"), "surface": "user"})
    m = _SPA_SESSION_RE.match(path)
    if m:
        kind = m.group("kind")
        sid = m.group("sid")
        extra: dict[str, object] = {"session_id": sid, "surface": "operator" if kind != "session" else "user"}
        return (kind, extra)
    return None


def _spa_response(page_kind: str, **extra_bootstrap: object) -> Response:
    """Build the SPA shell HTML with a bootstrap JSON payload."""
    import json as _json

    bootstrap: dict[str, object] = {
        "page_kind": page_kind,
        "title": "Provide Terminal",
        "app_path": "/app",
        "assets_path": "/assets",
    }
    bootstrap.update(extra_bootstrap)
    blob = _json.dumps(bootstrap).replace("</", "<\\/")
    # Session/operator/replay pages need hijack.js loaded before the SPA bundle.
    pre_scripts = ""
    page_script = "server-session-page.js"
    if page_kind in {"session", "operator", "inspect"}:
        pre_scripts = "<script type='module' src='/assets/hijack.js'></script>"
    elif page_kind == "replay":
        page_script = "server-replay-page.js"
    html = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>{bootstrap['title']}</title>"
        "<link rel='stylesheet' href='/assets/server-app-foundation.css'>"
        "<link rel='stylesheet' href='/assets/server-app-layout.css'>"
        "<link rel='stylesheet' href='/assets/server-app-components.css'>"
        "<link rel='stylesheet' href='/assets/server-app-views.css'>"
        f"<link rel='stylesheet' href='{_XTERM_CDN}/css/xterm.css'>"
        f"<link href='{_FONTS_CDN}' rel='stylesheet'>"
        f"<script src='{_XTERM_CDN}/lib/xterm.js'></script>"
        f"<script src='{_FITADDON_CDN}/lib/addon-fit.js'></script>"
        f"</head><body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"<script type='application/json' id='app-bootstrap'>{blob}</script>"
        f"{pre_scripts}"
        f"<script type='module' src='/assets/{page_script}'></script>"
        "</body></html>"
    )
    return Response(html, status=200, headers={"content-type": "text/html; charset=utf-8"})


def _has_cf_service_token(request: object) -> bool:
    """Check if request carries CF Access service token headers.

    When a Service Auth policy matches, CF Access validates the token and
    forwards the request.  The presence of CF-Access-Client-Id means CF
    Access already approved the request — the worker can trust it.

    In Pyodide, request.headers is a JS Headers proxy.  .get() may return
    a JS string or None.  We stringify and check length to be safe.
    """
    try:
        headers = request.headers  # type: ignore[union-attr]
        for name in ("cf-access-client-id", "CF-Access-Client-Id"):
            val = str(headers.get(name) or "")
            if val.endswith(".access"):
                return True
    except Exception:
        pass
    return False


async def _require_jwt(request: object, config: CloudflareConfig) -> Response | None:
    """Return a 401 Response if JWT auth fails, or ``None`` if auth passes.

    Skipped when auth mode is not ``jwt``, or when a CF Access service
    token is present (already validated by CF Access Service Auth policy).
    """
    if config.jwt.mode != "jwt":
        return None
    if _has_cf_service_token(request):
        return None
    token = extract_bearer_or_cookie(request)
    if not token:
        return json_response({"error": "authentication required"}, status=401)
    try:
        await decode_jwt(token, config.jwt)
    except JwtValidationError as exc:
        return json_response({"error": "invalid token", "detail": str(exc)}, status=401)
    return None


async def _decode_jwt_principal(request: object, config: CloudflareConfig) -> object | None:
    """Decode the caller's principal for ownership/role checks.

    Returns ``None`` in ``none``/``dev`` mode (open access — no enforcement).

    In ``jwt`` mode, accepts every auth path the middleware already trusts:

    * ``Cf-Access-Authenticated-User-Email`` → synthesized Principal with
      the email as subject_id (role: ``viewer``) — CF Access already
      validated the end-user identity upstream.
    * ``CF-Access-Client-Id`` (suffix ``.access``) → synthesized Principal
      with ``service:<client_id>`` as subject_id and ``admin`` role —
      service tokens are deployed with machine-to-machine intent and
      don't carry user-level scopes.
    * App JWT bearer/cookie → ``decode_jwt`` (handles public_key_pem AND
      jwks_url via Web Crypto).

    Previously this function only decoded app JWTs, which meant a request
    authenticated by CF Access Service Auth passed ``_require_jwt`` but
    then collapsed to ``principal=None`` downstream — bulk delete and
    ownerless session creation were executed as if the caller were anonymous.
    """
    if config.jwt.mode in {"none", "dev"}:
        return None
    # CF Access authenticated user
    email = _read_header(
        request,
        "cf-access-authenticated-user-email",
        "Cf-Access-Authenticated-User-Email",
    )
    if email:
        from provide.terminal.cloudflare.auth.jwt import Principal as _Principal

        return _Principal(subject_id=email, roles=("viewer",))
    # CF Access service token
    client_id = _read_header(request, "cf-access-client-id", "CF-Access-Client-Id")
    if client_id.endswith(".access"):
        from provide.terminal.cloudflare.auth.jwt import Principal as _Principal

        return _Principal(subject_id=f"service:{client_id}", roles=("admin",))
    token = extract_bearer_or_cookie(request)
    if not token:
        return None
    try:
        return await decode_jwt(token, config.jwt)
    except JwtValidationError:
        return None


async def _handle_sessions(request: object, env: object, config: CloudflareConfig) -> Response:
    """Handle GET/DELETE /api/sessions."""
    method = str(getattr(request, "method", "GET")).upper()
    if method == "DELETE":
        # Bulk delete is admin-only: any JWT principal must carry the admin role.
        principal = await _decode_jwt_principal(request, config)
        if principal is not None and "admin" not in principal.roles:  # type: ignore[union-attr]
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
    sessions = await list_kv_sessions(env)
    if principal is not None and "admin" not in principal.roles:  # type: ignore[union-attr]
        is_operator = "operator" in principal.roles  # type: ignore[union-attr]
        subject_id = principal.subject_id  # type: ignore[union-attr]

        def _can_read(s: dict) -> bool:  # type: ignore[type-arg]
            if s.get("owner") == subject_id:
                return True
            vis = s.get("visibility", "public")
            if vis == "public":
                return True
            if vis == "operator" and is_operator:
                return True
            return False

        sessions = [s for s in sessions if _can_read(s)]
    elif principal is None:
        # No JWT (open/dev mode) — return all sessions as-is.
        pass
    scope = "fleet" if kv_configured else "local"
    return json_response(sessions, headers={"X-Sessions-Scope": scope})


async def _handle_connect(request: object, env: object, config: CloudflareConfig) -> Response:
    """Handle POST /api/connect — create a session in KV."""
    import json as _json
    import uuid

    method = str(getattr(request, "method", "GET")).upper()
    if method != "POST":
        return json_response({"error": "method not allowed"}, status=405)
    try:
        raw = await request.json()  # type: ignore[union-attr]
        body = raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception:
        body = {}
    connector_type = str(body.get("connector_type", "shell"))
    prefix = "ushell" if connector_type == "ushell" else "connect"
    session_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    display_name = str(body.get("display_name") or session_id)
    input_mode = str(body.get("input_mode", "open"))
    tags = list(body.get("tags") or [])
    created_at = time.time()
    principal = await _decode_jwt_principal(request, config)
    owner = principal.subject_id if principal is not None else None  # type: ignore[union-attr]
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


async def _handle_session_delete(request: object, env: object, sid: str, config: CloudflareConfig) -> Response:
    """Handle DELETE /api/sessions/{id}."""
    principal = await _decode_jwt_principal(request, config)
    if principal is not None:
        # In JWT mode, verify the caller is the session owner or an admin.
        # KV is the auth source — a missing row means the session doesn't exist;
        # fail closed with 404 rather than letting the delete proceed unauthenticated.
        session_data = await get_kv_session(env, sid)
        if session_data is None:
            return json_response({"error": "not_found"}, status=404)
        session_owner = session_data.get("owner")
        is_admin = "admin" in principal.roles  # type: ignore[union-attr]
        is_owner = session_owner is not None and principal.subject_id == session_owner  # type: ignore[union-attr]
        if not is_admin and not is_owner:
            return json_response({"error": "forbidden"}, status=403)
    # Attempt DO cleanup before removing the KV entry so a failed DO cleanup
    # doesn't orphan a live DO while the session disappears from all API views.
    namespace = getattr(env, "SESSION_RUNTIME", None)
    if namespace is not None:
        try:
            stub = namespace.get(namespace.idFromName(sid))
            await stub.fetch(request)
        except Exception as _exc:
            return json_response({"error": "do_cleanup_failed", "detail": str(_exc)}, status=500)
    await delete_kv_session(env, sid)
    return json_response({"ok": True, "session_id": sid, "deleted": True})


async def _route_request(request: object, env: object, config: CloudflareConfig) -> Response:
    """Route an incoming request to the appropriate handler."""
    path = urlparse(str(request.url)).path

    # Public routes (no auth).
    if path == "/api/health":
        return json_response({"ok": True, "service": "provide-terminal-cloudflare", "environment": config.environment})
    if path.startswith("/assets/"):
        return serve_asset(path.removeprefix("/assets/"))
    if _STATIC_ASSET_PATH.match(path):
        return serve_asset(path.removeprefix("/"))

    try:
        from provide.terminal.cloudflare.api._tunnel_api import resolve_share_context
    except ImportError:  # pragma: no cover
        from api._tunnel_api import resolve_share_context  # type: ignore[import-not-found]

    spa = _resolve_spa_route(path)
    if spa is not None and spa[0] == "share" and "session_id" in spa[1]:
        # /s/{id} → 302 redirect to /app/{inspect|session}/{id}
        sid = str(spa[1]["session_id"])
        page = "session"
        kv_s = getattr(env, "SESSION_REGISTRY", None)
        if kv_s is not None:
            try:
                import json as _json_s

                raw_s = await kv_s.get(f"session:{sid}")
                if raw_s is not None:
                    page = str(_json_s.loads(str(raw_s)).get("share_page", "session"))
            except Exception:
                pass
        qs = urlparse(str(request.url)).query
        target = f"/app/{page}/{sid}"
        if qs:
            target += f"?{qs}"
        return Response(None, status=302, headers={"location": target})

    if spa is not None and "session_id" in spa[1]:
        share_context = await resolve_share_context(request, env, str(spa[1]["session_id"]), config)
        if share_context is not None:
            _, share_role = share_context
            # Use the URL-requested page kind (inspect, replay, session, operator)
            # rather than the token-derived kind from resolve_share_context.
            page_kind = spa[0]
            return _spa_response(
                page_kind,
                session_id=str(spa[1]["session_id"]),
                surface="operator" if share_role == "operator" else "user",
                share_role=share_role,
                share_token=(parse_qs(urlparse(str(request.url)).query).get("token", [None]) or [None])[0],
            )

    # Authenticated API routes.
    handler = _match_api_route(path, request)
    if handler is not None:
        auth_error = await _require_jwt(request, config)
        if auth_error is not None:
            return auth_error
        return await handler(request, env, config)

    # DO-proxied routes (includes /tunnel/{id} for WSS upgrade).
    worker_id = _extract_worker_id(path)
    if worker_id is not None:
        namespace = getattr(env, "SESSION_RUNTIME", None)
        if namespace is None:
            return json_response({"error": "SESSION_RUNTIME binding missing"}, status=500)
        return await namespace.get(namespace.idFromName(worker_id)).fetch(request)  # pragma: no cover

    return json_response({"error": "not_found", "path": path}, status=404)


def _match_api_route(path: str, request: object) -> object | None:
    """Return the handler coroutine for an authenticated route, or None."""
    if path == "/api/sessions":
        return _api_sessions
    if path == "/api/connect":
        return _api_connect
    if path == "/api/tunnels":
        return _api_tunnels
    rotate_match = _TUNNEL_TOKENS_ROTATE_RE.match(path)
    if rotate_match:
        tid = rotate_match.group("tunnel_id")
        return lambda req, env, cfg: _api_tunnel_rotate(req, env, cfg, tid)
    revoke_match = _TUNNEL_TOKENS_RE.match(path)
    if revoke_match:
        method = str(getattr(request, "method", "GET")).upper()
        if method == "DELETE":
            tid = revoke_match.group("tunnel_id")
            return lambda req, env, cfg: _api_tunnel_revoke(req, env, cfg, tid)
    if path == "/api/pam-events":
        return _api_pam_events
    if path.startswith("/api/profiles"):
        return _api_profiles
    session_delete_match = _SESSION_ID_RE.match(path)
    if session_delete_match and str(getattr(request, "method", "GET")).upper() == "DELETE":
        # Stash the match for the handler.
        return lambda req, env, cfg: _handle_session_delete(req, env, session_delete_match.group("session_id"), cfg)
    spa = _resolve_spa_route(path)
    if spa is not None:
        return lambda _req, _env, _cfg: _as_future(_spa_response(spa[0], **spa[1]))
    return None


async def _api_sessions(request: object, env: object, config: CloudflareConfig) -> Response:
    return await _handle_sessions(request, env, config)


async def _api_connect(request: object, env: object, config: CloudflareConfig) -> Response:
    return await _handle_connect(request, env, config)


async def _api_tunnels(request: object, env: object, config: CloudflareConfig) -> Response:
    try:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels
    except ImportError:  # pragma: no cover
        from api._tunnel_api import handle_tunnels  # type: ignore[import-not-found]

    principal = await _decode_jwt_principal(request, config)
    return await handle_tunnels(request, env, principal)


async def _api_tunnel_revoke(request: object, env: object, config: CloudflareConfig, tunnel_id: str) -> Response:
    try:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens
    except ImportError:  # pragma: no cover
        from api._tunnel_api import handle_tunnel_revoke_tokens  # type: ignore[import-not-found]

    principal = await _decode_jwt_principal(request, config)
    return await handle_tunnel_revoke_tokens(request, env, tunnel_id, principal)


async def _api_tunnel_rotate(request: object, env: object, config: CloudflareConfig, tunnel_id: str) -> Response:
    try:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens
    except ImportError:  # pragma: no cover
        from api._tunnel_api import handle_tunnel_rotate_tokens  # type: ignore[import-not-found]

    principal = await _decode_jwt_principal(request, config)
    return await handle_tunnel_rotate_tokens(request, env, tunnel_id, principal, ttl_s=config.tunnel_token_ttl_s)


async def _api_pam_events(request: object, env: object, _config: CloudflareConfig) -> Response:
    try:
        from provide.terminal.cloudflare.api._pam import handle_pam_event
    except ImportError:  # pragma: no cover
        from api._pam import handle_pam_event  # type: ignore[import-not-found]

    return await handle_pam_event(request, env)


async def _api_profiles(request: object, env: object, config: CloudflareConfig) -> Response:
    from provide.terminal.cloudflare.api._profiles import route_profiles

    path = urlparse(str(getattr(request, "url", ""))).path
    method = str(getattr(request, "method", "GET")).upper()
    # In dev mode, use a fixed principal. In JWT mode, resolve from token.
    principal_id = "dev-user" if config.jwt.mode == "dev" else await _resolve_principal_id(request, config)
    return await route_profiles(request, env, path, method, principal_id)


def _read_header(request: object, *names: str) -> str:
    """Read the first non-empty value of ``names`` from ``request.headers``."""
    try:
        headers = request.headers  # type: ignore[attr-defined]
    except Exception:
        return ""
    for name in names:
        try:
            val = str(headers.get(name) or "")
        except Exception:
            continue
        if val:
            return val
    return ""


async def _resolve_principal_id(request: object, config: CloudflareConfig) -> str:
    """Extract principal subject_id on a pre-authenticated request.

    Supports every path the auth layer already accepts:

    * CF Access authenticated user → ``Cf-Access-Authenticated-User-Email``
    * CF Access service token      → ``CF-Access-Client-Id`` (suffix ``.access``)
    * App JWT bearer/cookie        → ``decode_jwt`` (handles both
      ``public_key_pem`` AND ``jwks_url``; the previous implementation used
      sync PyJWT with ``public_key_pem`` only, silently degrading every
      JWKS-based deployment to ``anonymous`` ownership on profile CRUD.)

    Returns ``"anonymous"`` only when none of those produce an identity.
    """
    email = _read_header(
        request,
        "cf-access-authenticated-user-email",
        "Cf-Access-Authenticated-User-Email",
    )
    if email:
        return email
    client_id = _read_header(request, "cf-access-client-id", "CF-Access-Client-Id")
    if client_id.endswith(".access"):
        return f"service:{client_id}"
    token = extract_bearer_or_cookie(request)
    if not token:
        return "anonymous"
    try:
        principal = await decode_jwt(token, config.jwt)
        return str(principal.subject_id or "anonymous")
    except JwtValidationError:
        return "anonymous"


async def _as_future(value: Response) -> Response:
    return value


_STRICT_DEFAULTS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; "
            "font-src fonts.gstatic.com; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:"
        ),
    ),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)

_DEV_DEFAULTS: tuple[tuple[str, str], ...] = (("X-Content-Type-Options", "nosniff"),)

_OVERRIDE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", "security_csp"),
    ("Strict-Transport-Security", "security_hsts"),
    ("X-Frame-Options", "security_x_frame_options"),
    ("X-Content-Type-Options", "security_x_content_type_options"),
    ("Referrer-Policy", "security_referrer_policy"),
    ("Permissions-Policy", "security_permissions_policy"),
)


def _resolve_security_headers(config: CloudflareConfig) -> list[tuple[str, str]]:
    """Compute the list of (header-name, value) pairs to apply based on config.

    Resolution order:
    1. Start with mode defaults (strict or dev).
    2. Per-field override: if the config field is not None, replace (non-empty)
       or suppress (empty string) the header.
    """
    mode_defaults = _STRICT_DEFAULTS if config.security_mode != "dev" else _DEV_DEFAULTS
    result: dict[str, str] = dict(mode_defaults)
    for header_name, field_name in _OVERRIDE_FIELDS:
        override = getattr(config, field_name, None)
        if override is None:
            continue
        if override == "":
            result.pop(header_name, None)
        else:
            result[header_name] = override
    return list(result.items())


def _apply_security_headers(response: Response, config: CloudflareConfig) -> Response:
    """Add security headers to an HTTP response based on config.

    Works with both the real CF Runtime Headers object (has .set()) and the
    test stub (plain dict).  WebSocket 101 responses should not be passed here.
    """
    headers = _resolve_security_headers(config)
    resp_headers = getattr(response, "headers", None)
    if resp_headers is None:
        return response
    set_fn = getattr(resp_headers, "set", None)
    if callable(set_fn):
        for name, value in headers:
            set_fn(name, value)
    else:
        # Plain dict (test stub)
        for name, value in headers:
            resp_headers[name] = value
    return response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if not hasattr(self, "_config"):
            if _import_error:  # pragma: no cover
                import logging as _l  # pragma: no cover

                _l.getLogger(__name__).error("IMPORT_FALLBACK:\n%s", _import_error)  # pragma: no cover
            self._config = CloudflareConfig.from_env(self.env)
        response = await _route_request(request, self.env, self._config)
        if getattr(response, "status", None) != 101:
            _apply_security_headers(response, self._config)
        return response


ProvideTerminalCloudflareWorker = Default


def _extract_worker_id(path: str) -> str | None:
    for pattern in _WORKER_ROUTE_PATTERNS:
        match = pattern.match(path)
        if match:
            return str(match.group("worker_id"))
    return None
