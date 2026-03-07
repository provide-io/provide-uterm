#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""HTML page routes for the hosted terminal server."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from provide.terminal.server.auth import resolve_http_principal
from provide.terminal.server.ui import operator_dashboard_html, replay_page_html, session_page_html


def create_page_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def operator_dashboard(request: Request) -> HTMLResponse:
        cfg = request.app.state.uterm_config
        response = HTMLResponse(operator_dashboard_html(cfg.server.title, cfg.ui.app_path, cfg.ui.assets_path))
        principal = resolve_http_principal(request, cfg.auth)
        response.set_cookie(cfg.auth.principal_cookie, principal.name)
        response.set_cookie(cfg.auth.surface_cookie, "operator")
        return response

    @router.get("/session/{session_id}", response_class=HTMLResponse)
    async def session_view(request: Request, session_id: str) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        principal = resolve_http_principal(request, cfg.auth)
        html = session_page_html(
            session.display_name, cfg.ui.assets_path, session_id, operator=False, app_path=cfg.ui.app_path
        )
        response = HTMLResponse(html)
        response.set_cookie(cfg.auth.principal_cookie, principal.name)
        response.set_cookie(cfg.auth.surface_cookie, "user")
        return response

    @router.get("/operator/{session_id}", response_class=HTMLResponse)
    async def operator_session(request: Request, session_id: str) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        principal = resolve_http_principal(request, cfg.auth)
        html = session_page_html(
            session.display_name, cfg.ui.assets_path, session_id, operator=True, app_path=cfg.ui.app_path
        )
        response = HTMLResponse(html)
        response.set_cookie(cfg.auth.principal_cookie, principal.name)
        response.set_cookie(cfg.auth.surface_cookie, "operator")
        return response

    @router.get("/replay/{session_id}", response_class=HTMLResponse)
    async def replay_view(request: Request, session_id: str) -> HTMLResponse:
        session = await request.app.state.uterm_registry.get_definition(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        cfg = request.app.state.uterm_config
        principal = resolve_http_principal(request, cfg.auth)
        html = replay_page_html(session.display_name, cfg.ui.assets_path, session_id, app_path=cfg.ui.app_path)
        response = HTMLResponse(html)
        response.set_cookie(cfg.auth.principal_cookie, principal.name)
        response.set_cookie(cfg.auth.surface_cookie, "operator")
        return response

    return router
