#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Principal resolution for the standalone terminal server."""

from __future__ import annotations

import asyncio
import re
import threading
import time  # noqa: F401  # re-exported so legacy ``auth.time`` monkeypatches reach the webhook replay clock
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.server.api_keys import canonical_tenant_id
from provide.uterm.server.audit import audit_event
from provide.uterm.server.auth_roles import (
    _DEFAULT_ROLE,  # noqa: F401  # re-exported for legacy server.auth import surface
    _KNOWN_ROLES,  # noqa: F401  # re-exported for legacy server.auth import surface
    _filter_known_roles,
)
from provide.uterm.server.auth_webhook import (
    _REPLAY_CACHE_MAX_ENTRIES,  # noqa: F401  # re-exported for legacy server.auth import surface
    WebhookIdentityProvider,
    _BoundedReplayCache,  # noqa: F401  # re-exported for legacy server.auth import surface
)
from provide.uterm.server.bridge.identity import IdentityProvider, Principal

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import Request, WebSocket

    from provide.uterm.server.models import AuthConfig

# Explicit re-export list for mypy strict mode. ``Principal`` is the
# canonical name callers reach for when they need the authenticated-user
# type; it lives in ``provide.uterm.server.bridge.identity`` but is also
# imported here so server-side modules don't need to know that.
# ``WebhookIdentityProvider`` and the role-allow-list helper now live in
# sibling modules (``auth_webhook`` / ``auth_roles``) but are re-exported here
# so the historical ``provide.uterm.server.auth`` import surface is unchanged.
__all__ = [
    "IdentityProvider",
    "LocalIdentityProvider",
    "Principal",
    "WebhookIdentityProvider",
    "extract_bearer_token",
    "resolve_http_principal",
]

logger = get_logger(__name__)


# Module-level cache: jwks_url → PyJWKClient instance.
# PyJWKClient fetches and caches the JWKS document internally; sharing one
# instance per URL avoids a redundant HTTP round-trip on every token validation.
# Capped at 16 entries — in practice this is always 1 (one issuer per deployment).
# Protected by a threading.Lock because _resolve_jwt_key may run in a worker
# thread (via resolve_principal's asyncio.to_thread) concurrently with the
# event-loop thread.
_JWKS_CLIENT_CACHE: dict[str, Any] = {}
_JWKS_CLIENT_CACHE_MAX = 16
_JWKS_CLIENT_CACHE_LOCK = threading.Lock()


def _provider(auth: AuthConfig, api_key_store: Any = None) -> LocalIdentityProvider:
    return LocalIdentityProvider(auth, api_key_store)


def _cookie_value(cookies: dict[str, str], key: str) -> str | None:
    value = cookies.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _roles_from_claims(claims: dict[str, Any], auth: AuthConfig) -> frozenset[str]:
    return _provider(auth)._roles_from_claims(claims)


def _scopes_from_claims(claims: dict[str, Any], auth: AuthConfig) -> frozenset[str]:
    return _provider(auth)._scopes_from_claims(claims)


def _resolve_jwt_key(token: str, auth: AuthConfig) -> Any:
    return _provider(auth)._resolve_jwt_key(token)


def _principal_from_jwt_token(token: str, auth: AuthConfig) -> Principal:
    return _provider(auth)._principal_from_jwt_token(token)


def _principal_from_header_auth(
    headers: Any,
    cookies: dict[str, str],
    auth: AuthConfig,
) -> Principal:
    return _provider(auth)._principal_from_header_auth(headers, cookies)


def _anonymous_principal() -> Principal:
    return Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())


def _resolve_principal(
    headers: Any,
    cookies: dict[str, str],
    auth: AuthConfig,
    api_key_store: Any,
) -> Principal:
    connection = _HeadersAndCookies(headers=headers, cookies=cookies)
    return _provider(auth, api_key_store).resolve_principal_sync(connection)  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]


@dataclass(slots=True)
class _HeadersAndCookies:
    headers: Any = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


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


class LocalIdentityProvider(IdentityProvider):
    """Standard AGPL RBAC implementation of IdentityProvider."""

    def __init__(self, auth: AuthConfig, api_key_store: Any = None):
        self.auth = auth
        self.api_key_store = api_key_store

    async def resolve_principal(self, connection: Request | WebSocket) -> Principal:
        # resolve_principal_sync can block on a synchronous JWKS network fetch
        # (PyJWKClient.get_signing_key_from_jwt on a cache miss), so offload it
        # to a worker thread rather than running it on the event loop.
        return await asyncio.to_thread(self.resolve_principal_sync, connection)

    def resolve_principal_sync(self, connection: Request | WebSocket) -> Principal:
        headers = getattr(connection, "headers", {})
        cookies = getattr(connection, "cookies", {})

        # API key authentication takes precedence (when enabled).
        api_key_principal = self._principal_from_api_key(headers)
        if api_key_principal is not None:
            return api_key_principal

        mode = str(self.auth.mode).strip().lower()
        if mode == "header":
            # Finding #4: when trusted_proxy_ips is configured, only callers
            # from those source IPs may set X-Uterm-Role.  Other callers fall
            # through to the anonymous principal (same shape as a missing JWT).
            trusted = list(getattr(self.auth, "trusted_proxy_ips", ()) or [])
            if trusted:
                source = self._connection_source_ip(connection)
                if source not in trusted:
                    logger.warning(
                        "header_auth_rejected_untrusted_source source=%s trusted=%s",
                        source,
                        sorted(trusted),
                    )
                    audit_event(
                        "auth.failure",
                        detail={"method": "header", "reason": "untrusted_source", "source_ip": source},
                    )
                    return self._anonymous_principal()
            return self._principal_from_header_auth(headers, cookies)
        if mode != "jwt":
            raise ValueError(f"unknown auth mode: {mode!r}")

        token = extract_bearer_token(headers) or self._cookie_value(cookies, self.auth.token_cookie)
        if not token:
            return self._anonymous_principal()
        try:
            principal = self._principal_from_jwt_token(token)
        except Exception as exc:
            logger.warning("jwt_auth_failed error=%s", exc)
            audit_event("auth.failure", detail={"error": str(exc)})
            return self._anonymous_principal()

        audit_event("auth.success", principal=principal.subject_id)
        return principal

    @staticmethod
    def _connection_source_ip(connection: Any) -> str:
        """Best-effort source IP for header-auth trust gating.

        Returns the immediate TCP peer (``connection.client.host``) — NEVER
        ``X-Forwarded-For`` (which the caller controls).  The point of
        ``trusted_proxy_ips`` is to authenticate the *transport peer*, not
        any header the peer claims.
        """
        client = getattr(connection, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        return str(host) if host else ""

    def _cookie_value(self, cookies: dict[str, str], key: str) -> str | None:
        value = cookies.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _roles_from_claims(self, claims: dict[str, Any]) -> frozenset[str]:
        raw = claims.get(self.auth.jwt_roles_claim)
        if isinstance(raw, str):
            pieces = [part.strip().lower() for part in re.split(r"[,\s]+", raw) if part.strip()]
        elif isinstance(raw, list):
            pieces = [str(part).strip().lower() for part in raw if str(part).strip()]
        else:
            pieces = []
        return _filter_known_roles(pieces)

    def _scopes_from_claims(self, claims: dict[str, Any]) -> frozenset[str]:
        raw = claims.get(self.auth.jwt_scopes_claim)
        if isinstance(raw, str):
            return frozenset(part.strip() for part in raw.split() if part.strip())
        if isinstance(raw, list):
            return frozenset(str(part).strip() for part in raw if str(part).strip())
        return frozenset()

    def _resolve_jwt_key(self, token: str) -> Any:
        if self.auth.jwt_jwks_url:
            import jwt

            url = self.auth.jwt_jwks_url
            with _JWKS_CLIENT_CACHE_LOCK:
                client = _JWKS_CLIENT_CACHE.get(url)
                if client is None:
                    if len(_JWKS_CLIENT_CACHE) >= _JWKS_CLIENT_CACHE_MAX:
                        evict_n = _JWKS_CLIENT_CACHE_MAX // 2
                        for _k in list(_JWKS_CLIENT_CACHE)[:evict_n]:
                            del _JWKS_CLIENT_CACHE[_k]
                    client = jwt.PyJWKClient(url, cache_keys=True, timeout=10)
                    _JWKS_CLIENT_CACHE[url] = client
            return client.get_signing_key_from_jwt(token).key
        if self.auth.jwt_public_key_pem:
            return self.auth.jwt_public_key_pem
        raise ValueError("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode")

    def _principal_from_jwt_token(self, token: str) -> Principal:
        import jwt

        key = self._resolve_jwt_key(token)
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(self.auth.jwt_algorithms),
            issuer=self.auth.jwt_issuer,
            audience=self.auth.jwt_audience,
            leeway=max(0, int(self.auth.clock_skew_seconds)),
            options={"require": ["sub", "exp"]},
        )
        subject = str(claims.get("sub", "")).strip()
        if not subject:
            raise ValueError("sub claim is required")
        raw_tenant_claim = claims.get(self.auth.jwt_tenant_claim)
        raw_tenant = "" if raw_tenant_claim is None else str(raw_tenant_claim).strip()
        tenant = canonical_tenant_id(raw_tenant)
        if raw_tenant and tenant is None:
            raise ValueError("invalid tenant_id claim")
        return Principal(
            subject_id=subject,
            tenant_id=tenant,
            roles=self._roles_from_claims(claims),
            scopes=self._scopes_from_claims(claims),
            claims=claims,
        )

    def _anonymous_principal(self) -> Principal:
        return Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())

    def _principal_from_header_auth(self, headers: Any, cookies: Any) -> Principal:
        principal = (
            headers.get(self.auth.principal_header)
            or self._cookie_value(cookies, self.auth.principal_cookie)
            or "anonymous"
        )
        role_raw = headers.get(self.auth.role_header) or self._cookie_value(cookies, self.auth.role_cookie) or ""
        roles = _filter_known_roles([role_raw])
        raw_tenant = headers.get(self.auth.tenant_header) or self._cookie_value(cookies, self.auth.tenant_cookie) or ""
        tenant = canonical_tenant_id(raw_tenant)
        # A supplied-but-invalid tenant fails closed to the anonymous principal.
        if raw_tenant.strip() and tenant is None:
            return self._anonymous_principal()
        return Principal(subject_id=str(principal), tenant_id=tenant, roles=roles, scopes=frozenset())

    def _principal_from_api_key(self, headers: Any) -> Principal | None:
        if not self.auth.api_keys_enabled:
            return None
        raw_key = str(headers.get("x-api-key", "")).strip()
        if not raw_key:
            return None
        if self.api_key_store is None:
            return None
        record = self.api_key_store.validate(raw_key)
        if record is None:
            logger.warning("api_key_auth_failed key_id=unknown")
            audit_event("auth.failure", detail={"method": "api_key"})
            return None
        # Multi-tenancy: a key must carry a valid tenant. Legacy tenant-less keys
        # (flat ``create``) and keys with a malformed tenant fail closed so the
        # request is treated as unauthenticated.
        tenant = canonical_tenant_id(record.tenant_id)
        if tenant is None:
            logger.warning(
                "api_key_auth_failed key_id=%s reason=missing_or_invalid_tenant",
                record.key_id,
            )
            audit_event(
                "auth.failure",
                detail={"method": "api_key", "key_id": record.key_id, "reason": "missing_or_invalid_tenant"},
            )
            return None
        # Finding #3: explicit scope→role mapping.  Empty scopes OR scopes that
        # do not contain a recognised role keyword (admin/operator/viewer) used
        # to fall through to ``roles=admin`` — a key minted without any scope
        # silently authenticated as admin, and a typo (``"administrator"``) did
        # the same.  Now: unknown / empty scopes reject the key outright so the
        # request is treated as unauthenticated.
        roles: frozenset[str]
        scopes: frozenset[str]
        if "admin" in record.scopes:
            roles = frozenset({"admin"})
            scopes = frozenset({"*"})
        elif "operator" in record.scopes:
            roles = frozenset({"operator"})
            scopes = frozenset({"*"})
        elif "viewer" in record.scopes:
            roles = frozenset({"viewer"})
            scopes = frozenset({"*"})
        else:
            logger.warning(
                "api_key_auth_failed key_id=%s reason=unrecognized_or_empty_scope scopes=%s",
                record.key_id,
                sorted(record.scopes),
            )
            audit_event(
                "auth.failure",
                detail={
                    "method": "api_key",
                    "key_id": record.key_id,
                    "reason": "unrecognized_or_empty_scope",
                },
            )
            return None
        audit_event("auth.success", principal=record.key_id, detail={"method": "api_key"})
        return Principal(
            subject_id=f"apikey:{record.key_id}",
            tenant_id=tenant,
            roles=roles,
            scopes=scopes,
            claims={"key_id": record.key_id, "key_name": record.name},
        )


def _principal_from_api_key(headers: Any, auth: AuthConfig, api_key_store: Any) -> Principal | None:
    """Backward-compatible module helper for API key principal resolution."""
    return LocalIdentityProvider(auth, api_key_store)._principal_from_api_key(headers)


class _AwaitablePrincipal:
    """Compatibility wrapper that acts like a Principal and can also be awaited."""

    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._principal, name)

    def __await__(self) -> Generator[Any, None, Principal]:
        async def _resolve() -> Principal:
            return self._principal

        return _resolve().__await__()


def _api_key_store_from(connection: object) -> Any:
    """Pull the per-app ApiKeyStore off ``connection.app.state`` when present."""
    app = getattr(connection, "app", None)
    state = getattr(app, "state", None) if app is not None else None
    return getattr(state, "uterm_api_key_store", None) if state is not None else None


def resolve_http_principal(request: Request, auth: AuthConfig) -> _AwaitablePrincipal:
    """Resolve a principal from a FastAPI/Starlette Request-like object."""
    idp = LocalIdentityProvider(auth, _api_key_store_from(request))
    return _AwaitablePrincipal(idp.resolve_principal_sync(request))


def resolve_ws_principal(websocket: WebSocket, auth: AuthConfig) -> _AwaitablePrincipal:
    """Resolve a principal from a FastAPI/Starlette WebSocket-like object."""
    idp = LocalIdentityProvider(auth, _api_key_store_from(websocket))
    return _AwaitablePrincipal(idp.resolve_principal_sync(websocket))
