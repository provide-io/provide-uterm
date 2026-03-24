from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Import handler base classes directly from workers — these MUST resolve to the
# real CF runtime classes (not stubs) so Cloudflare's Pyodide validation phase
# detects Default/SessionRuntime as registered event handlers.
_DurableObject: type = object  # type: ignore[assignment]
try:
    from workers import DurableObject as _DurableObject  # type: ignore[import-not-found]
    from workers import (
        Response,  # type: ignore[import-not-found]
        WorkerEntrypoint,  # type: ignore[import-not-found]
    )
except ImportError:
    # Outside CF runtime (tests / local dev): defer to cf_types (loaded below).
    Response = None  # type: ignore[assignment]
    WorkerEntrypoint = None  # type: ignore[assignment]

# Ensure the current directory, its parent, and python_modules are in sys.path
# for Cloudflare runtime.  Pyodide loads modules from /session/metadata/ and
# needs explicit path configuration.
_current_file = Path(__file__).resolve()
_current_dir = str(_current_file.parent)  # .../provide_terminal_cloudflare/
_parent_dir = str(_current_file.parent.parent)  # contains provide_terminal_cloudflare/ as package
_python_modules_dir = str(_current_file.parent.parent.parent / "python_modules")

# In CF runtime, wrangler may flatten src/ so that entry.py is at /session/
# and the package is at /session/provide_terminal_cloudflare/.  Add /session/
# (the grandparent) as well as the typical /session/metadata/ parent.
_import_error: str | None = None

for _path in [_parent_dir, _current_dir, _python_modules_dir]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from provide_terminal_cloudflare.auth.jwt import (
        JwtValidationError,
        decode_jwt,
        extract_bearer_or_cookie,
    )
    from provide_terminal_cloudflare.cf_types import json_response
    from provide_terminal_cloudflare.config import CloudflareConfig
    from provide_terminal_cloudflare.do.session_runtime import SessionRuntime
    from provide_terminal_cloudflare.state.registry import delete_kv_session, list_kv_sessions
    from provide_terminal_cloudflare.ui.assets import read_asset_text, serve_asset

    if Response is None:  # workers module wasn't available (test env)
        from provide_terminal_cloudflare.cf_types import Response, WorkerEntrypoint  # type: ignore[assignment,no-redef]
except Exception:
    try:
        from auth.jwt import (  # type: ignore[import-not-found]
            JwtValidationError,
            decode_jwt,
            extract_bearer_or_cookie,
        )
        from cf_types import json_response  # type: ignore[import-not-found]
        from config import CloudflareConfig  # type: ignore[import-not-found]
        from do.session_runtime import SessionRuntime  # type: ignore[import-not-found]
        from state.registry import delete_kv_session, list_kv_sessions  # type: ignore[import-not-found]
        from ui.assets import read_asset_text, serve_asset  # type: ignore[import-not-found]

        if Response is None:  # workers module wasn't available
            from cf_types import Response, WorkerEntrypoint  # type: ignore[assignment,no-redef,import-not-found]
    except Exception:
        # Last resort for Pyodide validation phase — stubs for non-handler imports.
        # WorkerEntrypoint/Response/DurableObject are imported directly from workers
        # above, so handler registration always succeeds.
        _import_error = f"paths={sys.path[:5]}"
        JwtValidationError = Exception  # type: ignore[assignment]

        def decode_jwt(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def extract_bearer_or_cookie(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        def json_response(*_a: object, **_k: object) -> None:  # type: ignore[assignment]
            return None

        CloudflareConfig = object  # type: ignore[assignment]

        class SessionRuntime(_DurableObject):  # type: ignore[assignment]  # pragma: no cover
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
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/hijack(?:/.*)?$"),
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/(?:input_mode|disconnect_worker)$"),
    re.compile(
        r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})(?:/(?:snapshot|events|mode|clear|analyze|restart))?$"
    ),
)
_STATIC_ASSET_PATH = re.compile(r"^/[a-zA-Z0-9._/-]+\.(?:html|css|js)$")
_SESSION_ID_RE = re.compile(r"^/api/sessions/(?P<session_id>[a-zA-Z0-9_-]{1,64})$")

_XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
_FITADDON_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
_FONTS_CDN = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"

# SPA route patterns → (page_kind, needs_session_id, extra_scripts).
_SPA_SESSION_RE = re.compile(r"^/app/(?P<kind>session|operator|replay)/(?P<sid>[a-zA-Z0-9_-]{1,64})$")


def _resolve_spa_route(path: str) -> tuple[str, dict[str, object]] | None:
    """Return (page_kind, extra_bootstrap) for SPA routes, or None."""
    if path in {"/", "/app", "/app/"}:
        return ("dashboard", {})
    if path in {"/app/connect", "/app/connect/"}:
        return ("connect", {})
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
    if page_kind in {"session", "operator"}:
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


async def _require_jwt(request: object, config: CloudflareConfig) -> Response | None:
    """Return a 401 Response if JWT auth fails, or ``None`` if auth passes.

    Skipped when auth mode is not ``jwt``.
    """
    if config.jwt.mode != "jwt":
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
    entry = {
        "session_id": session_id,
        "display_name": display_name,
        "connector_type": connector_type,
        "lifecycle_state": "waiting",
        "input_mode": input_mode,
        "connected": False,
        "auto_start": False,
        "tags": [],
        "recording_enabled": False,
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

    # Authenticated API routes.
    handler = _match_api_route(path, request)
    if handler is not None:
        auth_error = await _require_jwt(request, config)
        if auth_error is not None:
            return auth_error
        return await handler(request, env, config)

    # DO-proxied routes.
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


async def _as_future(value: Response) -> Response:
    return value


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if not hasattr(self, "_config"):
            self._config = CloudflareConfig.from_env(self.env)
        return await _route_request(request, self.env, self._config)


ProvideTerminalCloudflareWorker = Default


def _extract_worker_id(path: str) -> str | None:
    for pattern in _WORKER_ROUTE_PATTERNS:
        match = pattern.match(path)
        if match:
            return str(match.group("worker_id"))
    return None
