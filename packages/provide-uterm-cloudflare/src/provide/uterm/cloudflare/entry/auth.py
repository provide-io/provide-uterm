"""Authentication and authorization helpers for the CF Worker entrypoint.

Covers JWT enforcement (`_require_jwt`), CF Access service-token detection
(`_has_cf_service_token`), principal decoding for ownership checks
(`_decode_jwt_principal`, `_resolve_principal_id`), and a tolerant header
reader (`_read_header`).
"""

from __future__ import annotations

import logging

from provide.uterm.cloudflare.entry.fallback_stubs import (
    CloudflareConfig,
    JwtValidationError,
    Response,
    decode_jwt,
    extract_bearer_or_cookie,
    json_response,
)

logger = logging.getLogger(__name__)


def _has_cf_service_token(request: object) -> bool:
    """Check if request carries CF Access service token headers.

    When a Service Auth policy matches, CF Access validates the token and
    forwards the request.  The presence of CF-Access-Client-Id means CF
    Access already approved the request — the worker can trust it.

    In Pyodide, request.headers is a JS Headers proxy.  .get() may return
    a JS string or None.  We stringify and check length to be safe.
    """
    try:
        headers = request.headers  # type: ignore[attr-defined]
        for name in ("cf-access-client-id", "CF-Access-Client-Id"):
            val = str(headers.get(name) or "")
            if val.endswith(".access"):
                return True
    except Exception as exc:
        logger.debug("cf_service_token_header_check_failed: %s", exc)
    return False


async def _require_jwt(request: object, config: CloudflareConfig) -> Response | None:
    """Return a 401 Response if JWT auth fails, or ``None`` if auth passes.

    Skipped when auth mode is not ``jwt``, or when a CF Access service
    token is present (already validated by CF Access Service Auth policy).
    """
    if config.jwt.mode != "jwt":
        return None
    if _has_cf_service_token(request):
        return None
    token = extract_bearer_or_cookie(request)
    if not token:
        return json_response({"error": "authentication required"}, status=401)
    try:
        await decode_jwt(token, config.jwt)
    except JwtValidationError as exc:
        return json_response({"error": "invalid token", "detail": str(exc)}, status=401)
    return None


async def _decode_jwt_principal(request: object, config: CloudflareConfig) -> object | None:
    """Decode the caller's principal for ownership/role checks.

    Returns ``None`` in ``none``/``dev`` mode (open access — no enforcement).

    In ``jwt`` mode, accepts every auth path the middleware already trusts:

    * ``Cf-Access-Authenticated-User-Email`` → synthesized Principal with
      the email as subject_id (role: ``viewer``) — CF Access already
      validated the end-user identity upstream.
    * ``CF-Access-Client-Id`` (suffix ``.access``) → synthesized Principal
      with ``service:<client_id>`` as subject_id and ``admin`` role —
      service tokens are deployed with machine-to-machine intent and
      don't carry user-level scopes.
    * App JWT bearer/cookie → ``decode_jwt`` (handles public_key_pem AND
      jwks_url via Web Crypto).

    Previously this function only decoded app JWTs, which meant a request
    authenticated by CF Access Service Auth passed ``_require_jwt`` but
    then collapsed to ``principal=None`` downstream — bulk delete and
    ownerless session creation were executed as if the caller were anonymous.
    """
    if config.jwt.mode in {"none", "dev"}:
        return None
    # CF Access authenticated user
    email = _read_header(
        request,
        "cf-access-authenticated-user-email",
        "Cf-Access-Authenticated-User-Email",
    )
    if email:
        from provide.uterm.cloudflare.auth.jwt import Principal as _Principal

        principal: object = _Principal(subject_id=email, roles=("viewer",))
        return principal
    # CF Access service token
    client_id = _read_header(request, "cf-access-client-id", "CF-Access-Client-Id")
    if client_id.endswith(".access"):
        from provide.uterm.cloudflare.auth.jwt import Principal as _Principal

        service_principal: object = _Principal(subject_id=f"service:{client_id}", roles=("admin",))
        return service_principal
    token = extract_bearer_or_cookie(request)
    if not token:
        return None
    try:
        decoded: object = await decode_jwt(token, config.jwt)
        return decoded
    except JwtValidationError:
        return None


def _read_header(request: object, *names: str) -> str:
    """Read the first non-empty value of ``names`` from ``request.headers``."""
    try:
        headers = request.headers  # type: ignore[attr-defined]
    except Exception:
        return ""
    for name in names:
        try:
            val = str(headers.get(name) or "")
        except Exception:
            continue
        if val:
            return val
    return ""


async def _resolve_principal_id(request: object, config: CloudflareConfig) -> str:
    """Extract principal subject_id on a pre-authenticated request.

    Supports every path the auth layer already accepts:

    * CF Access authenticated user → ``Cf-Access-Authenticated-User-Email``
    * CF Access service token      → ``CF-Access-Client-Id`` (suffix ``.access``)
    * App JWT bearer/cookie        → ``decode_jwt`` (handles both
      ``public_key_pem`` AND ``jwks_url``; the previous implementation used
      sync PyJWT with ``public_key_pem`` only, silently degrading every
      JWKS-based deployment to ``anonymous`` ownership on profile CRUD.)

    Returns ``"anonymous"`` only when none of those produce an identity.
    """
    email = _read_header(
        request,
        "cf-access-authenticated-user-email",
        "Cf-Access-Authenticated-User-Email",
    )
    if email:
        return email
    client_id = _read_header(request, "cf-access-client-id", "CF-Access-Client-Id")
    if client_id.endswith(".access"):
        return f"service:{client_id}"
    token = extract_bearer_or_cookie(request)
    if not token:
        return "anonymous"
    try:
        principal = await decode_jwt(token, config.jwt)
        return str(principal.subject_id or "anonymous")
    except JwtValidationError:
        return "anonymous"


__all__ = [
    "_decode_jwt_principal",
    "_has_cf_service_token",
    "_read_header",
    "_require_jwt",
    "_resolve_principal_id",
]
