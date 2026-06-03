#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
"""HTML page routes for the hosted terminal server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from provide.uterm.server.auth import extract_bearer_token, resolve_http_principal
from provide.uterm.server.ui import (
    connect_page_html,
    inspect_page_html,
    operator_dashboard_html,
    replay_page_html,
    session_page_html,
)

if TYPE_CHECKING:
    from provide.uterm.server.models import ServerConfig

_SessionId = Annotated[str, Path(pattern=r"^[\w\-]+$")]


def _is_secure_request(request: Request) -> bool:
    # Trust X-Forwarded-Proto only when the app is behind a known reverse proxy.
    # If the app is deployed without a proxy, a client can forge this header to
    # manipulate the Secure flag on auth cookies.  This is acceptable because:
    # (a) cookies are also HttpOnly+SameSite=Lax, and (b) operators who run
    # without a reverse proxy should use HTTPS directly (request.url.scheme).
    forwarded_proto = str(request.headers.get("x-forwarded-proto", "")).lower()
    if "https" in forwarded_proto:
        return True
    return request.url.scheme == "https"


def _set_auth_cookie(
    response: HTMLResponse,
    key: str,
    value: str,
    *,
    secure: bool,
    samesite: Literal["lax", "strict", "none"] = "lax",
) -> None:
    response.set_cookie(
        key=key,
        value=value,
        secure=secure,
        httponly=True,
        samesite=samesite,
    )


def _set_page_cookies(
    response: HTMLResponse,
    request: Request,
    cfg: ServerConfig,
    principal_name: str,
    surface: str,
    *,
    secure: bool,
    session_id: str | None = None,
) -> None:
    _set_auth_cookie(response, cfg.auth.principal_cookie, principal_name, secure=secure)
    _set_auth_cookie(response, cfg.auth.surface_cookie, surface, secure=secure)
    if cfg.auth.mode == "jwt" and principal_name != "anonymous":
        token = extract_bearer_token(request.headers)
        if token:
            _set_auth_cookie(response, cfg.auth.token_cookie, token, secure=secure)
    # Persist tunnel share token as an HttpOnly cookie so subsequent
    # WebSocket auth rides on the cookie rather than a JS-readable token
    # embedded in the page JSON.  Prevents a compromised CDN asset (or any
    # XSS) from exfiltrating the live share token.
    share_token = getattr(request.state, "uterm_share_token", None)
    if share_token and session_id:
        tunnel_cfg = cfg.tunnel
        # Honour operator-configured cookie attributes for the tunnel cookie.
        # cookie_secure=False lets operators run without HTTPS in local dev.
        tunnel_secure = secure if tunnel_cfg.cookie_secure else False
        _set_auth_cookie(
            response,
            f"uterm_tunnel_{session_id}",
            str(share_token),
            secure=tunnel_secure,
            samesite=tunnel_cfg.cookie_samesite,
        )


def _share_role(request: Request) -> str | None:
    return getattr(request.state, "uterm_share_role", None)


def create_page_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def operator_dashboard(request: Request) -> HTMLResponse:
        cfg = request.app.state.uterm_config
        secure = _is_secure_request(request)
        response = HTMLResponse(
            operator_dashboard_html(
                cfg.server.title,
                cfg.ui.app_path,
                cfg.ui.assets_path,
                xterm_cdn=cfg.ui.xterm_cdn,
                fitaddon_cdn=cfg.ui.fitaddon_cdn,
                fonts_cdn=cfg.ui.fonts_cdn,
                xterm_cdn_integrity=cfg.ui.xterm_cdn_integrity,
                fitaddon_cdn_integrity=cfg.ui.fitaddon_cdn_integrity,
            )
        )
        principal = getattr(request.state, "uterm_principal", None) or await resolve_http_principal(request, cfg.auth)
        _set_page_cookies(response, request, cfg, principal.name, "operator", secure=secure)
        return response

    @router.get("/session/{session_id}", response_class=HTMLResponse)
    async def session_view(request: Request, session_id: _SessionId) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        secure = _is_secure_request(request)
        principal = getattr(request.state, "uterm_principal", None) or await resolve_http_principal(request, cfg.auth)
        authz = request.app.state.uterm_authz
        if not await authz.can_read_session(principal, session):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        html = session_page_html(
            session.display_name,
            cfg.ui.assets_path,
            session_id,
            operator=False,
            app_path=cfg.ui.app_path,
            share_role=_share_role(request),
            xterm_cdn=cfg.ui.xterm_cdn,
            fitaddon_cdn=cfg.ui.fitaddon_cdn,
            fonts_cdn=cfg.ui.fonts_cdn,
            xterm_cdn_integrity=cfg.ui.xterm_cdn_integrity,
            fitaddon_cdn_integrity=cfg.ui.fitaddon_cdn_integrity,
        )
        response = HTMLResponse(html)
        _set_page_cookies(response, request, cfg, principal.name, "user", secure=secure, session_id=session_id)
        return response

    @router.get("/operator/{session_id}", response_class=HTMLResponse)
    async def operator_session(request: Request, session_id: _SessionId) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        secure = _is_secure_request(request)
        principal = getattr(request.state, "uterm_principal", None) or await resolve_http_principal(request, cfg.auth)
        authz = request.app.state.uterm_authz
        if not await authz.can_read_session(principal, session):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        html = session_page_html(
            session.display_name,
            cfg.ui.assets_path,
            session_id,
            operator=True,
            app_path=cfg.ui.app_path,
            share_role=_share_role(request),
            xterm_cdn=cfg.ui.xterm_cdn,
            fitaddon_cdn=cfg.ui.fitaddon_cdn,
            fonts_cdn=cfg.ui.fonts_cdn,
            xterm_cdn_integrity=cfg.ui.xterm_cdn_integrity,
            fitaddon_cdn_integrity=cfg.ui.fitaddon_cdn_integrity,
        )
        response = HTMLResponse(html)
        _set_page_cookies(response, request, cfg, principal.name, "operator", secure=secure, session_id=session_id)
        return response

    @router.get("/replay/{session_id}", response_class=HTMLResponse)
    async def replay_view(request: Request, session_id: _SessionId) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        secure = _is_secure_request(request)
        principal = getattr(request.state, "uterm_principal", None) or await resolve_http_principal(request, cfg.auth)
        authz = request.app.state.uterm_authz
        if not await authz.can_read_session(principal, session):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        html = replay_page_html(
            session.display_name,
            cfg.ui.assets_path,
            session_id,
            app_path=cfg.ui.app_path,
            share_role=_share_role(request),
            xterm_cdn=cfg.ui.xterm_cdn,
            fitaddon_cdn=cfg.ui.fitaddon_cdn,
            fonts_cdn=cfg.ui.fonts_cdn,
            xterm_cdn_integrity=cfg.ui.xterm_cdn_integrity,
            fitaddon_cdn_integrity=cfg.ui.fitaddon_cdn_integrity,
        )
        response = HTMLResponse(html)
        _set_page_cookies(response, request, cfg, principal.name, "operator", secure=secure, session_id=session_id)
        return response

    @router.get("/inspect/{session_id}", response_class=HTMLResponse)
    async def inspect_view(request: Request, session_id: _SessionId) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        secure = _is_secure_request(request)
        principal = getattr(request.state, "uterm_principal", None) or await resolve_http_principal(request, cfg.auth)
        authz = request.app.state.uterm_authz
        if not await authz.can_read_session(principal, session):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        html = inspect_page_html(
            session.display_name,
            cfg.ui.assets_path,
            session_id,
            app_path=cfg.ui.app_path,
            share_role=_share_role(request),
            xterm_cdn=cfg.ui.xterm_cdn,
            fitaddon_cdn=cfg.ui.fitaddon_cdn,
            fonts_cdn=cfg.ui.fonts_cdn,
            xterm_cdn_integrity=cfg.ui.xterm_cdn_integrity,
            fitaddon_cdn_integrity=cfg.ui.fitaddon_cdn_integrity,
        )
        response = HTMLResponse(html)
        _set_page_cookies(response, request, cfg, principal.name, "operator", secure=secure, session_id=session_id)
        return response

    @router.get("/connect", response_class=HTMLResponse)
    async def connect_view(request: Request) -> HTMLResponse:
        cfg = request.app.state.uterm_config
        secure = _is_secure_request(request)
        principal = getattr(request.state, "uterm_principal", None) or await resolve_http_principal(request, cfg.auth)
        response = HTMLResponse(
            connect_page_html(
                cfg.server.title,
                cfg.ui.assets_path,
                cfg.ui.app_path,
                xterm_cdn=cfg.ui.xterm_cdn,
                fitaddon_cdn=cfg.ui.fitaddon_cdn,
                fonts_cdn=cfg.ui.fonts_cdn,
                xterm_cdn_integrity=cfg.ui.xterm_cdn_integrity,
                fitaddon_cdn_integrity=cfg.ui.fitaddon_cdn_integrity,
            )
        )
        _set_page_cookies(response, request, cfg, principal.name, "operator", secure=secure)
        return response

    return router
