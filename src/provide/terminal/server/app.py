#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""FastAPI application factory for the hosted terminal server."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketException, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection  # noqa: TC002
from starlette.staticfiles import StaticFiles

from provide.terminal.hijack.hub import TermHub
from provide.terminal.server.auth import resolve_http_principal, resolve_ws_principal
from provide.terminal.server.authorization import AuthorizationService
from provide.terminal.server.policy import SessionPolicyResolver
from provide.terminal.server.registry import SessionRegistry
from provide.terminal.server.routes.api import create_api_router
from provide.terminal.server.routes.pages import create_page_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

    from provide.terminal.server.models import ServerConfig

logger = logging.getLogger(__name__)

# Delay between FastAPI startup completing and the auto-start session loop
# beginning.  Gives the event loop time to finish route/middleware init.
_AUTO_START_DELAY_S = 0.15


def _validate_frontend_assets() -> None:
    required = (
        "hijack.html",
        "hijack.js",
        "hijack.css",
        "app/boot.js",
        "app/router.js",
        "app/state.js",
        "app/api.js",
        "app/views/dashboard-view.js",
        "app/views/operator-view.js",
        "app/views/replay-view.js",
        "app/views/session-view.js",
    )
    frontend_root = importlib.resources.files("provide.terminal") / "frontend"
    missing = [name for name in required if not (frontend_root / name).is_file()]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"missing required frontend assets: {joined}")


def _validate_auth_config(config: ServerConfig) -> None:
    mode = str(config.auth.mode).strip().lower()
    if mode in {"none", "dev"}:
        return
    # All authenticated modes (jwt, header, …) require a worker bearer token.
    if not config.auth.worker_bearer_token:
        raise ValueError(f"auth.worker_bearer_token is required when auth.mode='{mode}'")
    if mode != "jwt":
        return
    if not config.auth.jwt_algorithms:
        raise ValueError("auth.jwt_algorithms must not be empty when auth.mode='jwt'")
    if any(a.strip().lower() == "none" for a in config.auth.jwt_algorithms):
        raise ValueError("'none' is not permitted in auth.jwt_algorithms")
    if not config.auth.jwt_public_key_pem and not config.auth.jwt_jwks_url:
        raise ValueError("configure auth.jwt_public_key_pem or auth.jwt_jwks_url when auth.mode='jwt'")


def create_server_app(config: ServerConfig) -> FastAPI:
    """Create the standalone reference server application."""
    _validate_auth_config(config)
    _validate_frontend_assets()
    authz = AuthorizationService()
    policy = SessionPolicyResolver(config.auth, authz=authz)
    registry: SessionRegistry | None = None
    metrics: dict[str, int] = {
        "http_requests_total": 0,
        "http_requests_4xx_total": 0,
        "http_requests_5xx_total": 0,
        "http_requests_error_total": 0,
        "auth_failures_http_total": 0,
        "auth_failures_ws_total": 0,
        "ws_disconnect_total": 0,
        "ws_disconnect_worker_total": 0,
        "ws_disconnect_browser_total": 0,
        "hijack_conflicts_total": 0,
        "hijack_lease_expiries_total": 0,
        "hijack_acquires_total": 0,
        "hijack_releases_total": 0,
        "hijack_steps_total": 0,
    }

    def _inc_metric(name: str, value: int = 1) -> None:
        metrics[name] = metrics.get(name, 0) + value

    async def _require_authenticated(connection: HTTPConnection) -> None:
        # Workers authenticate with a raw bearer token, not a JWT.  Check it
        # before JWT resolution so a valid worker token is never mis-rejected as
        # anonymous when auth.mode='jwt'.
        if config.auth.worker_bearer_token:
            from provide.terminal.server.auth import _extract_bearer_token

            token = _extract_bearer_token(connection.headers)
            if token == config.auth.worker_bearer_token:
                from provide.terminal.server.auth import Principal

                connection.state.uterm_principal = Principal(
                    subject_id="worker", roles=frozenset({"admin"}), scopes=frozenset({"*"})
                )
                return
        if connection.scope.get("type") == "websocket":
            # JWT mode: JWKS key fetch may make a blocking HTTP call; offload to
            # a thread pool to avoid stalling the event loop.
            if config.auth.mode == "jwt":
                principal = await asyncio.to_thread(resolve_ws_principal, connection, config.auth)
            else:
                principal = resolve_ws_principal(connection, config.auth)
            connection.state.uterm_principal = principal
            if config.auth.mode not in {"none", "dev"} and principal.subject_id == "anonymous":
                _inc_metric("auth_failures_ws_total")
                logger.info("authn_denied surface=websocket")
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="authentication required")
            return
        if config.auth.mode == "jwt":
            principal = await asyncio.to_thread(resolve_http_principal, connection, config.auth)
        else:
            principal = resolve_http_principal(connection, config.auth)
        connection.state.uterm_principal = principal
        if config.auth.mode not in {"none", "dev"} and principal.subject_id == "anonymous":
            _inc_metric("auth_failures_http_total")
            logger.info("authn_denied surface=http")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")

    async def _resolve_browser_role(ws: WebSocket, worker_id: str) -> str:
        principal = getattr(ws.state, "uterm_principal", None)
        if principal is None:
            principal = resolve_ws_principal(ws, config.auth)
        session = await registry.get_definition(worker_id) if registry is not None else None
        if session is None:
            return "admin" if config.auth.mode in {"none", "dev"} else "viewer"
        if not authz.can_read_session(principal, session):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="insufficient privileges")
        return policy.role_for(principal, session)

    hub = TermHub(
        resolve_browser_role=_resolve_browser_role,
        on_metric=_inc_metric,
        worker_token=config.auth.worker_bearer_token,
    )
    registry = SessionRegistry(
        config.sessions,
        hub=hub,
        public_base_url=config.server.public_base_url,
        recording=config.recording,
        worker_bearer_token=config.auth.worker_bearer_token,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def _delayed_boot() -> None:
            # Yield to the event loop so FastAPI finishes its own startup tasks
            # (route registration, middleware init) before we connect sessions.
            await asyncio.sleep(_AUTO_START_DELAY_S)
            await registry.start_auto_start_sessions()

        boot_task = asyncio.create_task(_delayed_boot())
        boot_task.add_done_callback(
            lambda t: (
                logger.error("auto_start_sessions_failed error=%s", t.exception())
                if not t.cancelled() and t.exception() is not None
                else None
            )
        )
        try:
            yield
        finally:
            boot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await boot_task
            await registry.shutdown()

    app = FastAPI(title=config.server.title, lifespan=_lifespan)
    app.state.uterm_config = config
    app.state.uterm_policy = policy
    app.state.uterm_authz = authz
    app.state.uterm_hub = hub
    app.state.uterm_registry = registry
    app.state.uterm_metrics = metrics

    @app.middleware("http")
    async def _request_logging_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.uterm_request_id = request_id
        start = time.perf_counter()
        _inc_metric("http_requests_total")
        try:
            response = await call_next(request)
        except Exception:
            _inc_metric("http_requests_error_total")
            logger.exception(
                "http_request_failed request_id=%s method=%s path=%s",
                request_id,
                request.method,
                request.url.path,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000.0
        if response.status_code >= 500:
            _inc_metric("http_requests_5xx_total")
        elif response.status_code >= 400:
            _inc_metric("http_requests_4xx_total")
        response.headers["x-request-id"] = request_id
        logger.info(
            "http_request request_id=%s method=%s path=%s status=%d duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    app.include_router(hub.create_router(), dependencies=[Depends(_require_authenticated)])
    app.include_router(create_api_router(), dependencies=[Depends(_require_authenticated)])
    app.include_router(create_page_router(), prefix=config.ui.app_path, dependencies=[Depends(_require_authenticated)])

    if config.server.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    frontend_path = importlib.resources.files("provide.terminal") / "frontend"
    app.mount(config.ui.assets_path, StaticFiles(directory=str(frontend_path), html=False), name="uterm-assets")
    return app
