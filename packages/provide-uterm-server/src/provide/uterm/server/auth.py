#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Principal resolution for the standalone terminal server."""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.server.audit import audit_event
from provide.uterm.server.bridge.identity import IdentityProvider, Principal
from provide.uterm.server.tracing import inject_trace_context

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import Request, WebSocket

    from provide.uterm.server.models import AuthConfig

# Explicit re-export list for mypy strict mode. ``Principal`` is the
# canonical name callers reach for when they need the authenticated-user
# type; it lives in ``provide.uterm.server.bridge.identity`` but is also
# imported here so server-side modules don't need to know that.
__all__ = [
    "IdentityProvider",
    "LocalIdentityProvider",
    "Principal",
    "WebhookIdentityProvider",
    "extract_bearer_token",
    "resolve_http_principal",
]

logger = get_logger(__name__)
# Canonical RBAC role allow-list. Any role minted from an external/untrusted
# source (JWT claims, proxy headers, the webhook IDP) MUST be filtered to this
# set; anything outside it is dropped so a compromised issuer cannot inject a
# privileged role like ``superuser``/``root``.
_KNOWN_ROLES = frozenset({"viewer", "operator", "admin"})
# Fallback role applied when role filtering leaves nothing.
_DEFAULT_ROLE = "viewer"


def _filter_known_roles(roles: Any) -> frozenset[str]:
    """Filter an arbitrary roles iterable to the known allow-list.

    Cleans each entry (str, stripped, lower-cased), drops any role outside
    ``_KNOWN_ROLES``, and falls back to ``{_DEFAULT_ROLE}`` when the result is
    empty. Shared by the JWT, header and webhook-IDP role-resolution paths.
    """
    cleaned = {str(role).strip().lower() for role in roles if str(role).strip()}
    allowed = cleaned & _KNOWN_ROLES
    return frozenset(allowed) if allowed else frozenset({_DEFAULT_ROLE})


# Module-level cache: jwks_url → PyJWKClient instance.
# PyJWKClient fetches and caches the JWKS document internally; sharing one
# instance per URL avoids a redundant HTTP round-trip on every token validation.
# Capped at 16 entries — in practice this is always 1 (one issuer per deployment).
# Protected by a threading.Lock because _resolve_jwt_key runs inside asyncio.to_thread.
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
    return _provider(auth, api_key_store).resolve_principal_sync(connection)  # type: ignore[arg-type]


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
        return self.resolve_principal_sync(connection)

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
        return Principal(
            subject_id=subject,
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
        return Principal(subject_id=str(principal), roles=roles, scopes=frozenset())

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


_REPLAY_CACHE_MAX_ENTRIES = 4096


class _BoundedReplayCache:
    """Bounded, TTL-evicted cache of recently-seen response signatures.

    L9 replay protection (layer 1): records each verified IdP response
    ``signature`` against the wall-clock time it was first seen. A signature
    presented again while its first-seen timestamp is still within the
    freshness window (``max_age_s`` — the same window the response-signature
    timestamp check uses) is reported as a replay.

    Bounded two ways so an attacker cannot grow it without limit: stale
    entries (older than ``max_age_s``) are purged on every insert, and the
    structure is hard-capped at ``max_entries`` with oldest-first eviction
    (``OrderedDict`` FIFO). A signature is only meaningful for one freshness
    window anyway, so eviction never weakens protection — a replay outside the
    window is independently rejected by the signature timestamp check.

    HA caveat: this cache is PER PROVIDER INSTANCE (per process). It does not
    protect a multi-node / HA deployment where a captured response could be
    replayed against a *different* instance. The optional nonce binding
    (``require_response_nonce``) covers that case by binding the response to a
    per-request nonce.
    """

    def __init__(self, max_age_s: float, max_entries: int = _REPLAY_CACHE_MAX_ENTRIES) -> None:
        self._max_age_s = max_age_s
        self._max_entries = max_entries
        # signature -> first-seen wall-clock timestamp; FIFO for eviction.
        self._seen: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, signature: str) -> bool:
        return signature in self._seen

    def _purge_stale(self, now: float) -> None:
        # Entries are inserted in time order, so the oldest live at the front;
        # stop at the first entry still within the window.
        for sig in list(self._seen):
            if now - self._seen[sig] > self._max_age_s:
                del self._seen[sig]
            else:
                break

    def seen_or_record(self, signature: str, *, now: float) -> bool:
        """Return True if ``signature`` is a replay; otherwise record it.

        A signature counts as a replay when it was already recorded and its
        first-seen timestamp is within ``max_age_s`` of ``now``. Stale entries
        are purged first, so a signature whose window has elapsed is treated as
        fresh (and re-recorded).
        """
        self._purge_stale(now)
        if signature in self._seen and now - self._seen[signature] <= self._max_age_s:
            return True
        self._seen[signature] = now
        self._seen.move_to_end(signature)
        # Hard cap: evict oldest entries beyond the bound.
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return False


class WebhookIdentityProvider(IdentityProvider):
    """IdentityProvider that delegates resolution to an external webhook."""

    def __init__(
        self,
        url: str,
        secret: str | None = None,
        timeout_s: float = 2.0,
        on_failure: str = "deny",
        require_signed_response: bool = True,
        forward_headers: frozenset[str] | None = None,
        forward_cookies: frozenset[str] | None = None,
        require_response_nonce: bool = False,
    ):
        self.url = url
        self.secret = secret
        self.timeout_s = timeout_s
        # Finding #7: webhook-down behaviour.  ``"deny"`` (the default) returns
        # ``None`` so the caller falls through to anonymous and the request is
        # rejected by the auth gate.  ``"viewer"`` preserves the legacy
        # fail-open behaviour for callers that explicitly want it.
        if on_failure not in {"deny", "viewer"}:
            raise ValueError(f"on_failure must be 'deny' or 'viewer'; got {on_failure!r}")
        self.on_failure = on_failure
        # 1f: when True, the webhook's RESPONSE must carry a valid HMAC signature
        # (over the raw response bytes) or the resolution falls into ``on_failure``.
        self.require_signed_response = require_signed_response
        # 1d: only these request headers/cookies are forwarded to the external
        # IdP — never the full request set. Empty = forward nothing (secure
        # default); the factory passes the curated auth-credential allow-list.
        self.forward_headers = forward_headers if forward_headers is not None else frozenset()
        self.forward_cookies = forward_cookies if forward_cookies is not None else frozenset()
        # L9 layer 3: when True, the IdP RESPONSE must echo the per-request nonce
        # (matching) or the resolution falls into on_failure. Defaults False for
        # backward-compat — an IdP that doesn't echo the nonce is still protected
        # by the per-instance replay cache (layer 1). Set True for HA / strict
        # request↔response binding where the per-instance cache isn't shared.
        self.require_response_nonce = require_response_nonce
        # L9 layer 1: per-instance bounded replay cache keyed on the response
        # signature, windowed to the same freshness horizon the signature check
        # uses so the two reject overlapping (but together cover the full
        # threat: in-window verbatim replay AND out-of-window stale timestamp).
        from provide.uterm.server.webhook_signing import _DEFAULT_MAX_AGE_S

        self._replay_cache = _BoundedReplayCache(max_age_s=_DEFAULT_MAX_AGE_S)

    async def resolve_principal(self, connection: Request | WebSocket) -> Principal | None:
        import json

        import httpx

        from provide.uterm.server.webhook_signing import build_webhook_signature, verify_webhook_signature

        all_headers = dict(getattr(connection, "headers", {}))
        all_cookies = dict(getattr(connection, "cookies", {}))

        # 1d: forward only the curated allow-list of credentials. Header keys are
        # matched case-insensitively (Starlette/httpx lower-case keys, but a
        # mixed-case mapping may reach us in tests/embedders); cookies match by
        # exact name.
        headers = {k: v for k, v in all_headers.items() if k.lower() in self.forward_headers}
        cookies = {k: v for k, v in all_cookies.items() if k in self.forward_cookies}

        # L9 layer 2: a fresh per-request nonce, carried BOTH inside the signed
        # request payload (so the request signature covers it) and as the
        # X-Uterm-Nonce header (so a non-validating IdP can echo it without
        # parsing the body). If the IdP echoes it in its (signed) response, we
        # verify the echo matches — cryptographically binding the response to
        # THIS request, defeating replay even across instances/HA.
        nonce = secrets.token_urlsafe(16)
        payload = {
            "headers": headers,
            "cookies": cookies,
            "action": "resolve_principal",
            "nonce": nonce,
        }

        body = json.dumps(payload, separators=(",", ":")).encode()
        req_headers: dict[str, str] = {"Content-Type": "application/json", "X-Uterm-Nonce": nonce}
        if self.secret:
            ts = str(time.time())
            req_headers["X-Uterm-Timestamp"] = ts
            req_headers["X-Uterm-Signature"] = build_webhook_signature(self.secret, body, ts)
        # Propagate the active W3C trace context onto the IDP resolution call so
        # the auth hop joins the same distributed trace. Via provide.telemetry
        # (OpenTelemetry-optional) — no-op when no span is active.
        inject_trace_context(req_headers)

        try:
            from provide.uterm.server.egress import assert_webhook_target_allowed

            await assert_webhook_target_allowed(self.url)
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.url, content=body, headers=req_headers)
                resp.raise_for_status()

                # 1f: authenticate the RESPONSE itself. Verify the HMAC signature
                # over the RAW response bytes (resp.content) BEFORE trusting any
                # of its fields — a MITM/compromised transport could otherwise
                # forge a principal. A failed check raises into the except below
                # so the on_failure (deny/viewer) + audit path fires. The
                # principal is then built from json.loads(resp.content) so the
                # parsed data and the verified bytes can never diverge.
                if self.require_signed_response and not verify_webhook_signature(
                    self.secret or "",
                    resp.content,
                    resp.headers.get("X-Uterm-Signature"),
                    resp.headers.get("X-Uterm-Timestamp"),
                ):
                    raise ValueError("webhook IdP response signature verification failed")

                # L9 layer 1: reject a verbatim replay. A signature is only valid
                # for one freshness window, so an attacker can only replay a
                # captured (signature, timestamp) pair while the signature check
                # above still accepts it — exactly the window this cache covers.
                # The first delivery records the signature; an identical signature
                # presented again within the window is a replay → rejected. (Skip
                # when there is no signature header to key on; the verbatim-replay
                # vector only exists once a response is signed.)
                resp_sig = resp.headers.get("X-Uterm-Signature")
                if resp_sig and self._replay_cache.seen_or_record(resp_sig, now=time.time()):
                    raise ValueError("webhook IdP response replay detected")

                data = json.loads(resp.content)

                # L9 layer 2/3: bind the response to this request via the nonce.
                # ``echoed`` is what the IdP returned in its signed body.
                #   * require_response_nonce=True → the echo MUST be present and
                #     match (else reject) — strict request↔response binding.
                #   * require_response_nonce=False → a present echo must still
                #     match (a present-but-wrong nonce is an attack → reject); an
                #     absent echo falls through to the replay-cache protection.
                echoed = data.get("nonce")
                if self.require_response_nonce:
                    if echoed is None or not hmac.compare_digest(str(echoed), nonce):
                        raise ValueError("webhook IdP response nonce missing or mismatched")
                elif echoed is not None and not hmac.compare_digest(str(echoed), nonce):
                    raise ValueError("webhook IdP response nonce mismatched")

                # Filter roles to the known allow-list: a compromised or
                # MITM'd IDP webhook must not be able to mint a privileged role
                # (e.g. admin) outside the recognised set.
                return Principal(
                    subject_id=data["subject_id"],
                    roles=_filter_known_roles(data.get("roles", [_DEFAULT_ROLE])),
                    scopes=frozenset(data.get("scopes", [])),
                    claims=data.get("claims", {}),
                    display_name=data.get("display_name"),
                )
        except Exception as exc:
            logger.warning(
                "webhook_auth_failed url=%s error=%s on_failure=%s",
                self.url,
                exc,
                self.on_failure,
            )
            # Surface the fail-open/attack signal in the structured audit
            # trail. Deliberately exclude the signing secret and raw request
            # headers from the detail so they never reach the audit log.
            audit_event(
                "auth.webhook_idp_failure",
                detail={"url": self.url, "on_failure": self.on_failure, "error": str(exc)},
            )
            if self.on_failure == "viewer":
                return Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())
            return None


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
