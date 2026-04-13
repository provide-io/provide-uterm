#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Principal resolution for the standalone terminal server."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.terminal.server.audit import audit_event

if TYPE_CHECKING:
    from provide.terminal.server.models import AuthConfig

logger = get_logger(__name__)
# Module-level cache: jwks_url → PyJWKClient instance.
# PyJWKClient fetches and caches the JWKS document internally; sharing one
# instance per URL avoids a redundant HTTP round-trip on every token validation.
# Capped at 16 entries — in practice this is always 1 (one issuer per deployment).
# Protected by a threading.Lock because _resolve_jwt_key runs inside asyncio.to_thread.
_JWKS_CLIENT_CACHE: dict[str, Any] = {}
_JWKS_CLIENT_CACHE_MAX = 16
_JWKS_CLIENT_CACHE_LOCK = threading.Lock()


@dataclass(slots=True)
class Principal:
    """Resolved browser or API principal."""

    subject_id: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    claims: dict[str, Any] = field(default_factory=dict)
    display_name: str | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.subject_id


def _cookie_value(cookies: dict[str, str], key: str) -> str | None:
    value = cookies.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_bearer_token(headers: Any) -> str | None:
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

        url = auth.jwt_jwks_url
        with _JWKS_CLIENT_CACHE_LOCK:
            client = _JWKS_CLIENT_CACHE.get(url)
            if client is None:
                if len(_JWKS_CLIENT_CACHE) >= _JWKS_CLIENT_CACHE_MAX:
                    # Evict oldest half, preserving recently-active issuers.
                    evict_n = _JWKS_CLIENT_CACHE_MAX // 2
                    for _k in list(_JWKS_CLIENT_CACHE)[:evict_n]:
                        del _JWKS_CLIENT_CACHE[_k]
                client = jwt.PyJWKClient(url, cache_keys=True, timeout=10)
                _JWKS_CLIENT_CACHE[url] = client
        return client.get_signing_key_from_jwt(token).key
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
        options={"require": ["sub", "exp"]},
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
    display_name = str(headers.get("x-display-name", "")).strip() or None
    return Principal(subject_id=str(principal), roles=roles, scopes=frozenset({"*"}), display_name=display_name)


def _principal_from_api_key(headers: Any, auth: AuthConfig) -> Principal | None:
    """Check for X-API-Key header and validate against the store.

    Returns a Principal on success or None if no API key header is present
    or the key is invalid.  The store is resolved lazily from a callback
    to avoid a hard import-time dependency on the app layer.
    """
    if not auth.api_keys_enabled:
        return None
    raw_key = str(headers.get("x-api-key", "")).strip()
    if not raw_key:
        return None
    # The store is attached by the caller via the module-level hook.
    store = _api_key_store_hook()
    if store is None:
        return None
    record = store.validate(raw_key)
    if record is None:
        logger.warning("api_key_auth_failed key_id=unknown")
        audit_event("auth.failure", detail={"method": "api_key"})
        return None
    roles: frozenset[str]
    if "admin" in record.scopes:
        roles = frozenset({"admin"})
    elif "operator" in record.scopes:
        roles = frozenset({"operator"})
    elif record.scopes:
        roles = frozenset({"viewer"})
    else:
        # Empty scopes = full access
        roles = frozenset({"admin"})
    audit_event("auth.success", principal=record.key_id, detail={"method": "api_key"})
    return Principal(
        subject_id=f"apikey:{record.key_id}",
        roles=roles,
        scopes=record.scopes,
        claims={"key_id": record.key_id, "key_name": record.name},
    )


# Module-level hook for the API key store. Set by app.py at startup.


def _api_key_store_hook() -> Any:
    return None


def set_api_key_store_hook(hook: Any) -> None:
    """Register a callable that returns the active ApiKeyStore (or None)."""
    global _api_key_store_hook
    _api_key_store_hook = hook


def _resolve_principal(headers: Any, cookies: Any, auth: AuthConfig) -> Principal:
    # API key authentication takes precedence (when enabled).
    api_key_principal = _principal_from_api_key(headers, auth)
    if api_key_principal is not None:
        return api_key_principal
    mode = str(auth.mode).strip().lower()
    if mode in {"none", "dev"}:
        return _principal_from_local_mode(headers, cookies, auth)
    if mode == "header":
        return _principal_from_header_auth(headers, cookies, auth)
    if mode != "jwt":
        raise ValueError(f"unknown auth mode: {mode!r}")
    token = extract_bearer_token(headers) or _cookie_value(cookies, auth.token_cookie)
    if not token:
        return _anonymous_principal()
    try:
        principal = _principal_from_jwt_token(token, auth)
    except Exception as exc:
        logger.warning("jwt_auth_failed error=%s", exc)
        audit_event("auth.failure", detail={"error": str(exc)})
        return _anonymous_principal()
    audit_event("auth.success", principal=principal.subject_id)
    return principal


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
