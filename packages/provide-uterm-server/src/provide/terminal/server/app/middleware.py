#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HTTP middleware stacking for the hosted terminal server."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from fastapi.middleware.cors import CORSMiddleware

from provide.telemetry import TelemetryMiddleware, get_logger
from provide.terminal.server.security import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

    from provide.terminal.server.models import ServerConfig

logger = get_logger(__name__)


def install_request_logging_middleware(app: FastAPI, *, inc_metric: Callable[[str, int], None]) -> None:
    """Register the request-id / metrics / access-log middleware on ``app``.

    Stamps an ``x-request-id`` header on every response, increments per-status
    counters via ``inc_metric``, and emits a structured access-log line.
    """

    @app.middleware("http")
    async def _request_logging_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.uterm_request_id = request_id
        start = time.perf_counter()
        inc_metric("http_requests_total", 1)
        try:
            response = await call_next(request)
        except Exception:
            inc_metric("http_requests_error_total", 1)
            logger.exception(
                "http_request_failed request_id=%s method=%s path=%s",
                request_id,
                request.method,
                request.url.path,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000.0
        if response.status_code >= 500:
            inc_metric("http_requests_5xx_total", 1)
        elif response.status_code >= 400:
            inc_metric("http_requests_4xx_total", 1)
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


def install_cors_security_telemetry(app: FastAPI, *, config: ServerConfig) -> None:
    """Stack CORS (when configured), security headers, and telemetry middleware.

    CORS is added first so it is the outermost layer; SecurityHeadersMiddleware
    and TelemetryMiddleware follow.  Order matches the historical behavior of
    ``server/app.py``.
    """
    if config.server.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    app.add_middleware(SecurityHeadersMiddleware, config=config.security, auth_mode=config.auth.mode)
    app.add_middleware(TelemetryMiddleware)
