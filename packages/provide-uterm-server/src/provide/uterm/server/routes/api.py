#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/server/test_routes_mutation_killing.py (router-endpoint extraction, mocked Request).
"""HTTP API routes for the hosted server app.

This module aggregates sub-routers (sessions, tunnels, webhooks, SSE, API keys)
under the ``/api`` prefix and adds metrics endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from provide.uterm.server.routes.api_keys import create_api_keys_router
from provide.uterm.server.routes.sessions import create_sessions_router
from provide.uterm.server.routes.sse import create_sse_router
from provide.uterm.server.routes.tunnels import create_tunnels_router
from provide.uterm.server.routes.webhooks import create_webhook_router


def _require_metrics_auth(request: Request) -> None:
    """Reject the request with 401 when metrics auth is enabled and the caller
    is anonymous. No-op when ``security.metrics_require_auth`` is False (the
    default) or no config is present (unit-test request mocks)."""
    cfg = getattr(request.app.state, "uterm_config", None)
    if cfg is None or not cfg.security.metrics_require_auth:
        return
    from provide.uterm.server.auth import resolve_http_principal

    # resolve_http_principal returns an _AwaitablePrincipal whose attributes are
    # readable synchronously (no await needed for .subject_id).
    if resolve_http_principal(request, cfg.auth).subject_id == "anonymous":
        raise HTTPException(status_code=401, detail="authentication required for /metrics")


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")
    router.include_router(create_sse_router())
    router.include_router(create_webhook_router())
    router.include_router(create_api_keys_router())
    router.include_router(create_sessions_router())
    router.include_router(create_tunnels_router())

    @router.get("/metrics")
    async def metrics(request: Request) -> dict[str, object]:
        _require_metrics_auth(request)
        payload = getattr(request.app.state, "uterm_metrics", {})
        if not isinstance(payload, dict):
            payload = {}
        return {"metrics": payload}

    @router.get("/metrics/prometheus")
    async def metrics_prometheus(request: Request) -> PlainTextResponse:
        _require_metrics_auth(request)
        payload = getattr(request.app.state, "uterm_metrics", {})
        if not isinstance(payload, dict):
            payload = {}
        lines: list[str] = []
        for name in sorted(payload):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {payload[name]}")
        body = "\n".join(lines) + ("\n" if lines else "")
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    return router
