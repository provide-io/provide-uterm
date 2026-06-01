#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Token authentication middleware for the swarm manager."""

from __future__ import annotations

import hmac
import os
import re
from typing import Any
from urllib.parse import parse_qs

from fastapi.responses import JSONResponse
from provide.telemetry import get_logger

logger = get_logger(__name__)

# Worker-self-report routes: a low-privilege worker token (when configured) is
# accepted ONLY on these. Everything else is operator-only. Path params are
# matched as a single non-slash segment and the pattern is fully anchored so a
# trailing/leading segment (e.g. ``/agent/x/statusfoo`` or a nested id) never
# slips through. Keep these in sync with manager/routes/status.py (status) and
# manager/routes/agent_ops.py (register).
_WORKER_SELF_REPORT_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"/agent/[^/]+/status")),
    ("POST", re.compile(r"/agent/[^/]+/register")),
)


class TokenAuthMiddleware:
    """ASGI middleware that enforces a bearer token.

    Two static tokens are supported. The operator ``token`` authorizes every
    route. The optional low-privilege ``worker_token`` authorizes ONLY the
    worker-self-report routes (``POST /agent/{id}/status`` and
    ``POST /agent/{id}/register``); it is rejected on operator routes (spawn,
    kill, delete, restart, prune, GET reads, …). When ``worker_token`` is unset
    the self-report routes still require the operator token (backward compatible
    for operator-only deployments).

    Parameters
    ----------
    app:
        The inner ASGI application.
    token:
        The expected operator token value (authorizes everything).
    worker_token:
        Optional low-privilege token accepted ONLY on the worker-self-report
        routes. ``None`` disables the second token entirely.
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
        worker_token: str | None = None,
        public_paths: frozenset[str] | None = None,
        public_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self._app = app
        self._token = token
        self._worker_token = worker_token
        self._public_paths = public_paths or frozenset()
        self._public_prefixes = public_prefixes or ()

    def _is_public_path(self, path: str) -> bool:
        """Return True if *path* is exempt from token authentication."""
        return path in self._public_paths or any(path.startswith(p) for p in self._public_prefixes)

    def _is_worker_self_report_route(self, path: str, method: str) -> bool:
        """Return True only for the low-privilege worker-self-report routes."""
        return any(method == m and pattern.fullmatch(path) is not None for m, pattern in _WORKER_SELF_REPORT_ROUTES)

    def _is_authorized(self, provided: str, path: str, method: str) -> bool:
        """Return True if *provided* token may access (*method*, *path*).

        The operator token authorizes everything (timing-safe). The worker
        token, when configured, additionally authorizes ONLY the
        worker-self-report routes — never an operator route.
        """
        if hmac.compare_digest(provided, self._token):
            return True
        return bool(
            self._worker_token is not None
            and self._is_worker_self_report_route(path, method)
            and hmac.compare_digest(provided, self._worker_token)
        )

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

        method: str = scope.get("method", "")
        if not self._is_authorized(provided, path, method):
            if scope_type == "websocket":
                await receive()  # consume websocket.connect
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 4403})
            else:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


# Hosts treated as loopback for the unauthenticated-dev fallback.
# 0.0.0.0 is intentionally NOT loopback — it binds every interface and would
# expose an unauthenticated manager. The bandit-flagged sibling in this module
# is the listening side, not this set.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOW_UNAUTH_ENV_VAR = "UTERM_MANAGER_ALLOW_UNAUTHENTICATED"


def _is_loopback_bind(host: str | None) -> bool:
    """Return True if *host* refers to a loopback / local-only bind address."""
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def setup_auth(app: Any, *, env_var: str = "UTERM_MANAGER_API_TOKEN", config: Any = None) -> None:
    """Add token auth middleware.

    Behaviour when the token env var is unset:

    * If the manager binds to a loopback address (127.0.0.1 / localhost / ::1)
      the middleware is skipped with a warning — convenient for local dev.
    * If the bind host is non-loopback the function raises ``RuntimeError`` so
      an unauthenticated manager never accidentally listens on a routable
      interface in production.
    * Setting ``UTERM_MANAGER_ALLOW_UNAUTHENTICATED=1`` is an explicit opt-out
      that re-enables the old "log warning and skip" behaviour for users who
      operate the manager in an ephemeral container with its own network
      policy.
    """
    token = os.environ.get(env_var, "").strip()
    if not token:
        bind_host = getattr(config, "host", None) if config is not None else None
        allow_unauth = os.environ.get(_ALLOW_UNAUTH_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}
        if allow_unauth:
            logger.warning(
                "api_token_auth_disabled",
                hint=f"Set {env_var} to enable",
                reason="explicit_opt_out",
            )
            return
        # When no config (and therefore no bind host) is supplied we can't
        # tell whether the deployment is loopback or routable. Preserve the
        # legacy warn-and-skip behaviour so embedders that wire the app
        # themselves aren't broken; require an explicit, non-loopback bind
        # to trigger the hard error.
        if config is None or _is_loopback_bind(bind_host):
            logger.warning(
                "api_token_auth_disabled",
                hint=f"Set {env_var} to enable",
                reason="loopback_bind" if config is not None else "no_config",
                bind_host=bind_host,
            )
            return
        raise RuntimeError(
            f"Manager API token is required when binding to a non-loopback host "
            f"({bind_host!r}). Set the {env_var} environment variable, bind to "
            f"127.0.0.1/localhost/::1, or set {_ALLOW_UNAUTH_ENV_VAR}=1 to "
            f"explicitly run unauthenticated."
        )
    public_paths: frozenset[str] = frozenset()
    public_prefixes: tuple[str, ...] = ()
    worker_env_var = "UTERM_MANAGER_WORKER_TOKEN"
    if config is not None:
        public_paths = frozenset(config.auth_public_paths)
        public_prefixes = tuple(config.auth_public_prefixes)
        worker_env_var = getattr(config, "auth_worker_token_env_var", worker_env_var)
    # Optional low-privilege worker token. When unset (or whitespace-only) the
    # self-report routes still require the operator token (backward compatible).
    worker_token = os.environ.get(worker_env_var, "").strip() or None
    logger.info("api_token_auth_enabled", worker_token_scoped=worker_token is not None)
    app.add_middleware(
        TokenAuthMiddleware,
        token=token,
        worker_token=worker_token,
        public_paths=public_paths,
        public_prefixes=public_prefixes,
    )
