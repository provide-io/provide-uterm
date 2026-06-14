#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Authentication mixin for SessionRuntime.

Provides JWT decode, share-token validation, and per-request role/subject
resolution helpers used by the Durable Object's ``fetch`` and route
handlers.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.uterm.cloudflare.auth.jwt import (
        JwtValidationError,
        decode_jwt,
        extract_bearer_or_cookie,
    )
    from provide.uterm.cloudflare.auth.jwt import resolve_role as _resolve_jwt_role
    from provide.uterm.cloudflare.cf_types import Response
else:
    try:
        from provide.uterm.cloudflare.auth.jwt import (
            JwtValidationError,
            decode_jwt,
            extract_bearer_or_cookie,
        )
        from provide.uterm.cloudflare.auth.jwt import resolve_role as _resolve_jwt_role
        from provide.uterm.cloudflare.cf_types import Response
    except Exception:  # pragma: no cover
        from auth.jwt import (  # type: ignore[import-not-found,no-redef]
            JwtValidationError,
            decode_jwt,
            extract_bearer_or_cookie,
        )
        from auth.jwt import resolve_role as _resolve_jwt_role  # type: ignore[no-redef]
        from cf_types import Response  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


class _AuthMixin:
    """Mixin providing JWT/share-token authentication helpers for SessionRuntime."""

    def _share_role_for_request(self, request: object) -> str | None:
        ip_binding = bool(getattr(self.config, "tunnel_ip_binding", False))  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]

        token = None
        try:
            from http.cookies import SimpleCookie

            cookie_header = str(request.headers.get("cookie") or request.headers.get("Cookie") or "")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            cookies = SimpleCookie(cookie_header)
            cookie_key = f"uterm_tunnel_{self.worker_id}"  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            if cookie_key in cookies:
                token = cookies[cookie_key].value
        except Exception:
            pass
        if not token:
            return None

        # Constant-time hash compare; the DO holds digests, not plain tokens.
        from provide.uterm.tunnel.token_hash import verify_token

        role: str | None = None
        if self._control_token_hash and verify_token(token, self._control_token_hash):  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            role = "admin"
        elif self._share_token_hash and verify_token(token, self._share_token_hash):  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            role = "viewer"

        if role is None:
            return None

        # Issue-time expiry: a share/control cookie stops authorizing the DO's
        # WS/fetch path once the tunnel's TTL elapses, mirroring the Default
        # Worker's resolve_share_context gate. expires_at is set at
        # issuance/rotation (now + ttl_s, configurable per-tunnel, default 1h).
        expires_at = self._session_expires_at  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        if expires_at is not None and time.time() > expires_at:
            return None

        if ip_binding:
            issued_ip = self._issued_ip or ""  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            client_ip = ""
            try:
                client_ip = str(request.headers.get("CF-Connecting-IP") or "")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            except Exception:
                pass
            if issued_ip and client_ip != issued_ip:
                return None

        return role

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _extract_token(self, request: object) -> str | None:
        """Extract a token from Authorization header or CF_Authorization cookie."""
        token = extract_bearer_or_cookie(request)
        if token:
            return token
        return None

    async def resolve_principal(self, request: object) -> tuple[Any, Response | None]:
        """Validate JWT auth.

        Returns ``(principal, None)`` when auth succeeds or is not required
        (``none``/``dev`` mode), or ``(None, error_response)`` on failure.
        """
        if self.config.jwt.mode in {"none", "dev"}:  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            return None, None
        share_role = self._share_role_for_request(request)
        if share_role is not None:
            return None, None
        token = self._extract_token(request)
        if not token:
            return None, Response(
                json.dumps({"error": "authentication required"}, ensure_ascii=True),
                status=401,
                headers={"content-type": "application/json"},
            )
        try:
            principal = await decode_jwt(token, self.config.jwt)  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            return principal, None
        except JwtValidationError as exc:
            return None, Response(
                json.dumps({"error": "invalid token", "detail": str(exc)}, ensure_ascii=True),
                status=401,
                headers={"content-type": "application/json"},
            )

    async def _resolve_principal(self, request: object) -> tuple[Any, Response | None]:
        """Backward-compatible alias for older tests/callers."""
        return await self.resolve_principal(request)

    async def browser_role_for_request(self, request: object) -> str:
        """Return the caller's role string based on JWT, ownership, or auth mode.

        Returns ``"admin"`` in ``none``/``dev`` mode (open access). In ``jwt`` mode,
        decodes the token and returns ``"admin"``, ``"operator"``, or ``"viewer"``.
        Owners of a private session are elevated to ``"operator"`` when their
        JWT role is lower — matching the hosted FastAPI server's
        ``resolve_browser_role``.  Without this elevation an owner with a
        ``viewer``-role JWT could read their session via the visibility check
        but would get 403 on every mutation route (mode/hijack/…).
        Falls back to ``"viewer"`` if the token is missing or invalid (the token
        was already validated in ``fetch()``; this is only for role extraction).
        """
        if self.config.jwt.mode in {"none", "dev"}:  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            return "admin"
        share_role = self._share_role_for_request(request)
        if share_role is not None:
            return share_role
        token = self._extract_token(request)
        if not token:
            return "viewer"
        try:
            principal = await decode_jwt(token, self.config.jwt)  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        except JwtValidationError:
            return "viewer"
        # Other exceptions (e.g. network errors fetching JWKS) propagate so the
        # caller returns a 5xx rather than silently downgrading the caller to viewer.
        jwt_role = _resolve_jwt_role(principal)
        if jwt_role == "admin":
            return "admin"
        owner = self.meta.get("owner")  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        if owner is not None and principal.subject_id == owner:
            return "operator"
        return jwt_role

    async def browser_subject_for_request(self, request: object) -> str | None:
        """Return the JWT subject_id for the caller, or ``None`` in open-access modes.

        Used by session route handlers to check per-session ownership.
        """
        if self.config.jwt.mode in {"none", "dev"}:  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            return None
        token = self._extract_token(request)
        if not token:
            return None
        try:
            principal = await decode_jwt(token, self.config.jwt)  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            return principal.subject_id
        except JwtValidationError:
            return None
