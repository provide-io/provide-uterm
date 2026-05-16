#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Router include / mount wiring for the hosted terminal server."""

from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING

from fastapi import Depends
from fastapi import Request as FastAPIRequest
from starlette.staticfiles import StaticFiles

from provide.uterm.server.routes.api import create_api_router
from provide.uterm.server.routes.approvals import create_approvals_router
from provide.uterm.server.routes.health import create_health_router
from provide.uterm.server.routes.pages import create_page_router
from provide.uterm.server.routes.profiles import create_profiles_router

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI
    from starlette.requests import HTTPConnection

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.server.models import ServerConfig


def install_routers(
    app: FastAPI,
    *,
    config: ServerConfig,
    hub: TermHub,
    require_authenticated: Callable[[HTTPConnection], Awaitable[None]],
    require_hub_route_authz: Callable[[HTTPConnection], Awaitable[None]],
) -> None:
    """Mount the hub, health, api, profiles, approvals, and page routers."""
    # Tunnel routes are passed as extra registrars to avoid a hard import
    # dependency from bridge → tunnel (enables future package extraction).
    from provide.uterm.bridge.fanout._routes import register_fanout_routes
    from provide.uterm.tunnel.fastapi_routes import register_tunnel_routes

    app.include_router(
        hub.create_router(extra_route_registrars=[register_tunnel_routes, register_fanout_routes]),
        dependencies=[Depends(require_authenticated), Depends(require_hub_route_authz)],
    )
    app.include_router(create_health_router())
    app.include_router(create_api_router(), dependencies=[Depends(require_authenticated)])
    app.include_router(create_profiles_router(), dependencies=[Depends(require_authenticated)])
    app.include_router(create_approvals_router(), dependencies=[Depends(require_authenticated)])
    app.include_router(
        create_page_router(),
        prefix=config.ui.app_path,
        dependencies=[Depends(require_authenticated)],
    )

    @app.get("/s/{session_id}")
    async def short_share_url(request: FastAPIRequest, session_id: str) -> object:
        """Short share URL: /s/{id}?token=... → redirect to /app/{inspect|session}/{id}?token=..."""
        from starlette.responses import RedirectResponse

        tunnel_tokens: dict[str, dict[str, object]] = request.app.state.uterm_tunnel_tokens
        entry = tunnel_tokens.get(session_id, {})
        page = str(entry.get("share_page", "session"))
        qs = str(request.url.query)
        target = f"{config.ui.app_path}/{page}/{session_id}"
        if qs:
            target += f"?{qs}"
        return RedirectResponse(url=target, status_code=302)


def mount_frontend_assets(app: FastAPI, *, config: ServerConfig) -> None:
    """Mount the bundled xterm/UI static assets at ``config.ui.assets_path``."""
    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount(
        config.ui.assets_path,
        StaticFiles(directory=str(frontend_path), html=False),
        name="uterm-assets",
    )
