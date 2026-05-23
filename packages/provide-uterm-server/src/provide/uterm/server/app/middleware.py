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
from provide.uterm.server.security import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastapi import FastAPI
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send

    from provide.uterm.server.models import ServerConfig

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


class WebSocketOriginMiddleware:
    """Reject WebSocket upgrade requests whose ``Origin`` header isn't allowed.

    Browser CORS only applies to HTTP. A WebSocket handshake is an HTTP
    upgrade, but ``Access-Control-Allow-Origin`` is never consulted — a
    stale tab on any domain can open a WS to the server and probe its
    surface unless the server itself rejects unwanted origins at the
    101 upgrade.

    Behaviour:

    - Non-browser clients (no ``Origin`` header — Python ``websockets``,
      ``wscat``, server-to-server) are always passed through. The
      whole-stack threat model assumes those clients are authenticated
      via JWT or other identity frames, which is a separate concern.
    - **Same-origin** requests (Origin scheme://host:port matches the
      request's own Host) are always passed through. The threat model
      for this middleware is *cross*-origin WS hijack; same-origin is
      universally safe (the page and the server are already coupled by
      origin policy on every other surface). This means single-host
      deployments and the bundled demos work without an explicit
      ``allowed_origins`` list.
    - If a browser sends a *cross-origin* ``Origin`` header, it must
      match an entry in ``allowed_origins`` (exact, lowercased) —
      otherwise the handshake is closed with HTTP 4403 before any
      application code runs.
    - **Empty** ``allowed_origins`` means *deny all cross-origin browser
      requests*. Same-origin still passes (see above). This is the
      secure default for the cross-origin threat: if the operator has
      not enumerated *other* origins, none are accepted. (Before
      2026-05-22 this was a no-op; the flip is a deliberate posture
      change — see ``.provide/design`` notes.)
    - The literal entry ``"*"`` is the explicit operator opt-out
      ("anything goes") for dev/test deployments that want any-origin
      access.

    HTTP requests are passed through untouched — :class:`CORSMiddleware`
    handles those.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Sequence[str]) -> None:
        self._app = app
        # Normalise once at construction so the per-request check is a
        # plain set membership test. Strip trailing slashes the way
        # ``CORSMiddleware`` does so the two surfaces are consistent.
        self._allowed: set[str] = {o.rstrip("/").lower() for o in allowed_origins}
        self._wildcard: bool = "*" in self._allowed

    @staticmethod
    def _expected_same_origin(scope: Scope, host_header: str) -> str:
        """Return the canonical Origin form for ``host_header`` under this scope.

        ASGI WS scope reports scheme=ws/wss. Browsers send Origin with the
        HTTP equivalent (http/https), so map the scheme accordingly before
        comparing. The Host header may carry the port (which we preserve)
        or not (which we leave as-is — the Origin will be normalised the
        same way at lookup).
        """
        scheme = "https" if scope.get("scheme") == "wss" else "http"
        return f"{scheme}://{host_header.rstrip('/').lower()}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket" or self._wildcard:
            await self._app(scope, receive, send)
            return

        origin: str | None = None
        host_header: str | None = None
        for name, value in scope.get("headers", ()):
            if name == b"origin":
                origin = value.decode("latin-1").rstrip("/").lower()
            elif name == b"host":
                host_header = value.decode("latin-1").strip()

        if origin is None:
            # Non-browser client; auth lives elsewhere.
            await self._app(scope, receive, send)
            return

        # Same-origin is always permitted. The threat this middleware
        # exists to mitigate is *cross*-origin hijack of authenticated
        # sessions; if Origin matches the server's own host the request
        # came from a page the operator already trusts to load.
        if host_header and origin == self._expected_same_origin(scope, host_header):
            await self._app(scope, receive, send)
            return

        if origin in self._allowed:
            await self._app(scope, receive, send)
            return

        # Reject the WS handshake. Starlette's websocket close protocol
        # is: send an http.response.start + http.response.body pair with
        # an HTTP 403 status before the upgrade completes.
        logger.warning(
            "websocket_origin_rejected origin=%s allowed=%s",
            origin,
            sorted(self._allowed),
        )
        await send({"type": "websocket.close", "code": 4403, "reason": "origin not allowed"})


def install_cors_security_telemetry(app: FastAPI, *, config: ServerConfig) -> None:
    """Stack CORS, the WS-origin gate, security headers, and telemetry middleware.

    Middleware order (outermost → innermost):

    1. :class:`CORSMiddleware` — handles HTTP cross-origin headers.
    2. :class:`WebSocketOriginMiddleware` — enforces the same allowlist
       on the WS upgrade path that CORS won't see.
    3. :class:`SecurityHeadersMiddleware` — emits CSP/HSTS/etc.
    4. :class:`TelemetryMiddleware` — tracing + access logs.

    Both CORS and WS-origin gate read from ``config.server.allowed_origins``
    so a single config knob controls cross-origin posture across both
    HTTP and WebSocket surfaces.
    """
    if config.server.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    # Always install the WS origin gate; it's a no-op when allowed_origins
    # is empty, and a real gate when the operator has set it.
    app.add_middleware(WebSocketOriginMiddleware, allowed_origins=tuple(config.server.allowed_origins))

    app.add_middleware(SecurityHeadersMiddleware, config=config.security, auth_mode=config.auth.mode)
    app.add_middleware(TelemetryMiddleware)
