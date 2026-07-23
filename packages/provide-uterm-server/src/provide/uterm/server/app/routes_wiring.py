#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Router include / mount wiring for the hosted terminal server."""

from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import Depends
from fastapi import Request as FastAPIRequest
from starlette.staticfiles import StaticFiles

from provide.uterm.server.graphical_routes import create_graphical_router
from provide.uterm.server.routes.api import create_api_router
from provide.uterm.server.routes.approvals import create_approvals_router
from provide.uterm.server.routes.health import create_health_router
from provide.uterm.server.routes.pages import create_page_router

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI
    from starlette.requests import HTTPConnection

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.models import ServerConfig


def install_routers(
    app: FastAPI,
    *,
    config: ServerConfig,
    hub: TermHub,
    require_authenticated: Callable[[HTTPConnection], Awaitable[None]],
    require_hub_route_authz: Callable[[HTTPConnection], Awaitable[None]],
) -> None:
    """Mount the hub, health, api, approvals, and page routers."""
    # Tunnel routes are passed as extra registrars to avoid a hard import
    # dependency from bridge → tunnel (enables future package extraction).
    from provide.uterm.server.bridge.fanout._routes import register_fanout_routes
    from provide.uterm.tunnel.fastapi_routes import register_tunnel_routes

    app.include_router(
        hub.create_router(extra_route_registrars=[register_tunnel_routes, register_fanout_routes]),
        dependencies=[Depends(require_authenticated), Depends(require_hub_route_authz)],
    )
    # Health/liveness/readiness routes stay anonymous; the security-posture
    # route inside this router is gated by ``require_authenticated`` (passed
    # through) because it reveals the effective security config.
    app.include_router(create_health_router(require_authenticated=require_authenticated))
    app.include_router(create_api_router(), dependencies=[Depends(require_authenticated)])
    app.include_router(create_approvals_router(), dependencies=[Depends(require_authenticated)])
    # Graphical-target CRUD is gated by capability + tenant scope inside each
    # handler; require_authenticated ensures a resolved principal is present.
    app.include_router(create_graphical_router(), dependencies=[Depends(require_authenticated)])
    app.include_router(
        create_page_router(),
        prefix=config.ui.app_path,
        dependencies=[Depends(require_authenticated)],
    )

    @app.get("/s/{session_id}")
    async def short_share_url(request: FastAPIRequest, session_id: str) -> object:
        """Short share URL: /s/{id}?invite=... → set cookie and redirect cleanly."""
        from fastapi import HTTPException
        from starlette.responses import RedirectResponse

        from provide.uterm.server.tunnel_invites import (
            consume_tunnel_invite,
            tunnel_invite_matches_token_hash,
        )

        tunnel_tokens: dict[str, dict[str, object]] = request.app.state.uterm_tunnel_tokens
        tunnel_invites: dict[str, dict[str, object]] = request.app.state.uterm_tunnel_invites
        entry = tunnel_tokens.get(session_id, {})
        query = parse_qs(str(request.url.query))
        invite_value = (query.get("invite", [None]) or [None])[0]
        invite = None
        if invite_value:
            invite = consume_tunnel_invite(tunnel_invites, invite_value, session_id=session_id)
            if invite is None:
                raise HTTPException(status_code=403, detail="invalid or expired invite")
            token_hash_key = "control_token_hash" if invite.role == "operator" else "share_token_hash"
            if not tunnel_invite_matches_token_hash(invite, str(entry.get(token_hash_key, ""))):
                raise HTTPException(status_code=403, detail="stale invite")

        page = (
            "operator" if invite is not None and invite.role == "operator" else str(entry.get("share_page", "session"))
        )
        target = f"{config.ui.app_path}/{page}/{session_id}"
        response = RedirectResponse(url=target, status_code=302)
        if invite is not None:
            # The Secure flag is taken from static config (tunnel.cookie_secure,
            # default True) — never from request.url.scheme or the spoofable
            # X-Forwarded-Proto header. An untrusted peer must not be able to
            # flip a cookie to/from Secure by forging the forwarded-proto header.
            response.set_cookie(
                key=f"uterm_tunnel_{session_id}",
                value=invite.tunnel_token,
                secure=config.tunnel.cookie_secure,
                httponly=True,
                samesite=config.tunnel.cookie_samesite,
            )
        return response


def mount_frontend_assets(app: FastAPI, *, config: ServerConfig) -> None:
    """Mount the bundled xterm/UI static assets at ``config.ui.assets_path``."""
    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount(
        config.ui.assets_path,
        StaticFiles(directory=str(frontend_path), html=False),
        name="uterm-assets",
    )
