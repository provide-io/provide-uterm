"""SPA route resolution and HTML shell rendering."""

from __future__ import annotations

import json as _json
import re

from provide.uterm.cloudflare.entry.fallback_stubs import Response

_XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
_FITADDON_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
_FONTS_CDN = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"

# SPA route patterns → (page_kind, needs_session_id, extra_scripts).
_SPA_SESSION_RE = re.compile(r"^/app/(?P<kind>session|operator|replay|inspect)/(?P<sid>[a-zA-Z0-9_-]{1,64})$")
_SHARE_ROUTE_RE = re.compile(r"^/s/(?P<sid>[a-zA-Z0-9_-]{1,64})$")


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
    return Response(html, status=200, headers={"content-type": "text/html; charset=utf-8"})  # ty:ignore[call-non-callable]


__all__ = ["_SHARE_ROUTE_RE", "_SPA_SESSION_RE", "_resolve_spa_route", "_spa_response"]
