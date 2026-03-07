#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Principal resolution for the standalone terminal server."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.terminal.server.models import AuthConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Principal:
    """Resolved browser or API principal."""

    subject_id: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.subject_id


def _cookie_value(cookies: dict[str, str], key: str) -> str | None:
    value = cookies.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_bearer_token(headers: Any) -> str | None:
    authorization = str(headers.get("authorization", "")).strip()
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _roles_from_claims(claims: dict[str, Any], auth: AuthConfig) -> frozenset[str]:
    raw = claims.get(auth.jwt_roles_claim)
    if isinstance(raw, str):
        pieces = [part.strip().lower() for part in re.split(r"[,\s]+", raw) if part.strip()]
    elif isinstance(raw, list):
        pieces = [str(part).strip().lower() for part in raw if str(part).strip()]
    else:
        pieces = []
    cleaned = [role for role in pieces if role in {"viewer", "operator", "admin"}]
    if not cleaned:
        cleaned = ["viewer"]
    return frozenset(cleaned)


def _scopes_from_claims(claims: dict[str, Any], auth: AuthConfig) -> frozenset[str]:
    raw = claims.get(auth.jwt_scopes_claim)
    if isinstance(raw, str):
        return frozenset(part.strip() for part in raw.split() if part.strip())
    if isinstance(raw, list):
        return frozenset(str(part).strip() for part in raw if str(part).strip())
    return frozenset()


def _resolve_jwt_key(token: str, auth: AuthConfig) -> Any:
    if auth.jwt_jwks_url:
        import jwt

        return jwt.PyJWKClient(auth.jwt_jwks_url).get_signing_key_from_jwt(token).key
    if auth.jwt_public_key_pem:
        return auth.jwt_public_key_pem
    raise ValueError("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode")


def _principal_from_jwt_token(token: str, auth: AuthConfig) -> Principal:
    import jwt

    key = _resolve_jwt_key(token, auth)
    claims = jwt.decode(
        token,
        key=key,
        algorithms=list(auth.jwt_algorithms),
        issuer=auth.jwt_issuer,
        audience=auth.jwt_audience,
        leeway=max(0, int(auth.clock_skew_seconds)),
        options={"require": ["sub", "exp", "iat", "nbf"]},
    )
    subject = str(claims.get("sub", "")).strip()
    if not subject:
        raise ValueError("sub claim is required")
    return Principal(
        subject_id=subject,
        roles=_roles_from_claims(claims, auth),
        scopes=_scopes_from_claims(claims, auth),
        claims=claims,
    )


def _anonymous_principal() -> Principal:
    return Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())


def _principal_from_header_auth(headers: Any, cookies: Any, auth: AuthConfig) -> Principal:
    principal = headers.get(auth.principal_header) or _cookie_value(cookies, auth.principal_cookie) or "anonymous"
    role = str(headers.get(auth.role_header, "")).strip().lower()
    roles = frozenset({role}) if role in {"viewer", "operator", "admin"} else frozenset({"viewer"})
    return Principal(subject_id=str(principal), roles=roles, scopes=frozenset())


def _principal_from_local_mode(headers: Any, cookies: Any, auth: AuthConfig) -> Principal:
    principal = headers.get(auth.principal_header) or _cookie_value(cookies, auth.principal_cookie) or "local-dev"
    role = str(headers.get(auth.role_header, "")).strip().lower()
    roles = frozenset({role}) if role in {"viewer", "operator", "admin"} else frozenset({"admin"})
    return Principal(subject_id=str(principal), roles=roles, scopes=frozenset({"*"}))


def _resolve_principal(headers: Any, cookies: Any, auth: AuthConfig) -> Principal:
    mode = str(auth.mode).strip().lower()
    if mode in {"none", "dev"}:
        return _principal_from_local_mode(headers, cookies, auth)
    if mode == "header":
        return _principal_from_header_auth(headers, cookies, auth)
    if mode != "jwt":
        logger.warning("unknown_auth_mode mode=%s; falling back to anonymous", mode)
        return _anonymous_principal()
    token = _extract_bearer_token(headers) or _cookie_value(cookies, auth.token_cookie)
    if not token:
        return _anonymous_principal()
    try:
        return _principal_from_jwt_token(token, auth)
    except Exception as exc:
        logger.warning("jwt_auth_failed error=%s", exc)
        return _anonymous_principal()


def resolve_http_principal(request: object, auth: AuthConfig) -> Principal:
    """Resolve a principal from a FastAPI/Starlette Request-like object."""
    headers = getattr(request, "headers", {})
    cookies = getattr(request, "cookies", {})
    return _resolve_principal(headers, cookies, auth)


def resolve_ws_principal(websocket: object, auth: AuthConfig) -> Principal:
    """Resolve a principal from a FastAPI/Starlette WebSocket-like object."""
    headers = getattr(websocket, "headers", {})
    cookies = getattr(websocket, "cookies", {})
    return _resolve_principal(headers, cookies, auth)
