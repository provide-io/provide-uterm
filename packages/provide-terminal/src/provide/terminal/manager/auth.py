#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Token authentication middleware for the swarm manager."""

from __future__ import annotations

import hmac
import os
from typing import Any
from urllib.parse import parse_qs

from fastapi.responses import JSONResponse
from provide.telemetry import get_logger

logger = get_logger(__name__)


class TokenAuthMiddleware:
    """ASGI middleware that enforces a bearer token.

    Parameters
    ----------
    app:
        The inner ASGI application.
    token:
        The expected token value.
    public_paths:
        Exact paths that bypass auth.
    public_prefixes:
        Path prefixes that bypass auth.
    """

    def __init__(
        self,
        app: Any,
        token: str,
        *,
        public_paths: frozenset[str] | None = None,
        public_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self._app = app
        self._token = token
        self._public_paths = public_paths or frozenset()
        self._public_prefixes = public_prefixes or ()

    def _is_public_path(self, path: str) -> bool:
        """Return True if *path* is exempt from token authentication."""
        return path in self._public_paths or any(path.startswith(p) for p in self._public_prefixes)

    def _extract_request_token(self, scope: Any) -> tuple[str, bool]:
        """Extract bearer token from scope. Returns (token, pass_through).

        pass_through=True means the request should bypass auth (e.g. OPTIONS).
        """
        scope_type = scope.get("type")
        if scope_type == "websocket":
            qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
            return (parse_qs(qs).get("token") or [""])[0].strip(), False
        method: str = scope.get("method", "")
        if method == "OPTIONS":
            return "", True
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if auth.startswith("Bearer "):
            return auth[len("Bearer ") :].strip(), False
        return headers.get(b"x-api-token", b"").decode("utf-8", errors="replace").strip(), False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if self._is_public_path(path):
            await self._app(scope, receive, send)
            return

        provided, pass_through = self._extract_request_token(scope)
        if pass_through:
            await self._app(scope, receive, send)
            return

        if not hmac.compare_digest(provided, self._token):
            if scope_type == "websocket":
                await receive()  # consume websocket.connect
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 4403})
            else:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def setup_auth(app: Any, *, env_var: str = "UTERM_MANAGER_API_TOKEN", config: Any = None) -> None:
    """Add token auth middleware if the env var is set."""
    token = os.environ.get(env_var, "").strip()
    if not token:
        logger.warning("api_token_auth_disabled", hint=f"Set {env_var} to enable")
        return
    public_paths: frozenset[str] = frozenset()
    public_prefixes: tuple[str, ...] = ()
    if config is not None:
        public_paths = frozenset(config.auth_public_paths)
        public_prefixes = tuple(config.auth_public_prefixes)
    logger.info("api_token_auth_enabled")
    app.add_middleware(
        TokenAuthMiddleware,
        token=token,
        public_paths=public_paths,
        public_prefixes=public_prefixes,
    )
