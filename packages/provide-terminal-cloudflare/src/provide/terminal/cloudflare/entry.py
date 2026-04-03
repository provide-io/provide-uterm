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
    from provide.terminal.cloudflare.state.registry import delete_kv_session, list_kv_sessions
    from provide.terminal.cloudflare.ui.assets import read_asset_text, serve_asset
except Exception:
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
        from state.registry import delete_kv_session, list_kv_sessions  # type: ignore[import-not-found]
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


__all__ = ["Default", "SessionRuntime", "ProvideTerminalCloudflareWorker"]

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
    except Exception:  # noqa: S110
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


async def _handle_sessions(request: object, env: object, _config: CloudflareConfig) -> Response:
    """Handle GET/DELETE /api/sessions."""
    method = str(getattr(request, "method", "GET")).upper()
    if method == "DELETE":
        kv = getattr(env, "SESSION_REGISTRY", None)
        if kv is None:
            return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)
        keys_resp = await kv.list()
        keys = [k.name for k in keys_resp.keys]
        for key in keys:
            await kv.delete(key)
        return json_response({"ok": True, "deleted": len(keys)})
    kv_configured = getattr(env, "SESSION_REGISTRY", None) is not None
    sessions = await list_kv_sessions(env)
    scope = "fleet" if kv_configured else "local"
    return json_response(sessions, headers={"X-Sessions-Scope": scope})


async def _handle_connect(request: object, env: object) -> Response:
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
        "owner": None,
        "visibility": "public",
        "last_error": None,
    }
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is not None:
        await kv.put(f"session:{session_id}", _json.dumps({**entry, "hijacked": False}))
    return json_response({**entry, "url": f"/app/session/{session_id}"})


async def _handle_session_delete(request: object, env: object, sid: str) -> Response:
    """Handle DELETE /api/sessions/{id}."""
    await delete_kv_session(env, sid)
    namespace = getattr(env, "SESSION_RUNTIME", None)
    if namespace is not None:
        import contextlib as _contextlib

        with _contextlib.suppress(Exception):
            stub = namespace.get(namespace.idFromName(sid))
            await stub.fetch(request)
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
    except ImportError:
        from api._tunnel_api import resolve_share_context  # type: ignore[import-not-found]

    spa = _resolve_spa_route(path)
    if spa is not None and spa[0] == "share" and "session_id" in spa[1]:
        # /s/{id} → 302 redirect to /app/session/{id} (parity with FastAPI)
        sid = str(spa[1]["session_id"])
        qs = urlparse(str(request.url)).query
        target = f"/app/session/{sid}"
        if qs:
            target += f"?{qs}"
        return Response(None, status=302, headers={"location": target})

    if spa is not None and "session_id" in spa[1]:
        share_context = await resolve_share_context(request, env, str(spa[1]["session_id"]))
        if share_context is not None:
            page_kind, share_role = share_context
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
        return await namespace.get(namespace.idFromName(worker_id)).fetch(request)

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
            return lambda req, env, _cfg: _api_tunnel_revoke(req, env, tid)
    if path == "/api/pam-events":
        return _api_pam_events
    if path.startswith("/api/profiles"):
        return _api_profiles
    session_delete_match = _SESSION_ID_RE.match(path)
    if session_delete_match and str(getattr(request, "method", "GET")).upper() == "DELETE":
        # Stash the match for the handler.
        return lambda req, env, _cfg: _handle_session_delete(req, env, session_delete_match.group("session_id"))
    spa = _resolve_spa_route(path)
    if spa is not None:
        return lambda _req, _env, _cfg: _as_future(_spa_response(spa[0], **spa[1]))
    return None


async def _api_sessions(request: object, env: object, config: CloudflareConfig) -> Response:
    return await _handle_sessions(request, env, config)


async def _api_connect(request: object, env: object, _config: CloudflareConfig) -> Response:
    return await _handle_connect(request, env)


async def _api_tunnels(request: object, env: object, _config: CloudflareConfig) -> Response:
    try:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnels
    except ImportError:
        from api._tunnel_api import handle_tunnels  # type: ignore[import-not-found]

    return await handle_tunnels(request, env)


async def _api_tunnel_revoke(request: object, env: object, tunnel_id: str) -> Response:
    try:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens
    except ImportError:
        from api._tunnel_api import handle_tunnel_revoke_tokens  # type: ignore[import-not-found]

    return await handle_tunnel_revoke_tokens(request, env, tunnel_id)


async def _api_tunnel_rotate(request: object, env: object, config: CloudflareConfig, tunnel_id: str) -> Response:
    try:
        from provide.terminal.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens
    except ImportError:
        from api._tunnel_api import handle_tunnel_rotate_tokens  # type: ignore[import-not-found]

    return await handle_tunnel_rotate_tokens(request, env, tunnel_id, ttl_s=config.tunnel_token_ttl_s)


async def _api_pam_events(request: object, env: object, _config: CloudflareConfig) -> Response:
    try:
        from provide.terminal.cloudflare.api._pam import handle_pam_event
    except ImportError:
        from api._pam import handle_pam_event  # type: ignore[import-not-found]

    return await handle_pam_event(request, env)


async def _api_profiles(request: object, env: object, config: CloudflareConfig) -> Response:
    from provide.terminal.cloudflare.api._profiles import route_profiles

    path = urlparse(str(getattr(request, "url", ""))).path
    method = str(getattr(request, "method", "GET")).upper()
    # In dev mode, use a fixed principal. In JWT mode, resolve from token.
    principal_id = "dev-user" if config.jwt.mode == "dev" else _resolve_principal_id(request, config)
    return await route_profiles(request, env, path, method, principal_id)


def _resolve_principal_id(request: object, config: CloudflareConfig) -> str:
    """Extract principal subject_id from JWT token on a pre-authenticated request.

    Uses synchronous PyJWT decode (not the async Web Crypto path) since this runs
    in the entry worker, not inside a DO.
    """
    from provide.terminal.cloudflare.auth.jwt import extract_bearer_or_cookie

    token = extract_bearer_or_cookie(request)
    if not token:
        return "anonymous"
    try:
        import jwt as pyjwt

        key = config.jwt.public_key_pem or ""
        claims = pyjwt.decode(token, key, algorithms=list(config.jwt.algorithms), options={"verify_exp": True})
        return str(claims.get("sub") or claims.get("common_name") or "anonymous")
    except Exception:
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
