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
    """Compatibility stub: raw CF Access headers are not trusted for auth."""
    try:
        _ = request.headers  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
    except Exception as exc:
        logger.debug("cf_service_token_header_check_failed: %s", exc)
    return False


async def _require_jwt(request: object, config: CloudflareConfig) -> Response | None:
    """Return a 401 Response if JWT auth fails, or ``None`` if auth passes.

    Skipped when auth mode is not ``jwt``.
    """
    if config.jwt.mode != "jwt":  # ty:ignore[unresolved-attribute]
        return None
    token = extract_bearer_or_cookie(request)
    if not token:
        return json_response({"error": "authentication required"}, status=401)
    try:
        await decode_jwt(token, config.jwt)  # ty:ignore[invalid-await, unresolved-attribute]
    except JwtValidationError as exc:
        return json_response({"error": "invalid token", "detail": str(exc)}, status=401)
    return None


async def _decode_jwt_principal(request: object, config: CloudflareConfig) -> object | None:
    """Decode the caller's principal for ownership/role checks.

    Returns ``None`` in ``none``/``dev`` mode (open access — no enforcement).

    In ``jwt`` mode, identity comes **only** from a cryptographically verified
    app JWT (bearer or cookie). Unsigned CF Access identity headers
    (``Cf-Access-Authenticated-User-Email``) are **not** trusted here: clients
    can set them on any request, and JWT-only deployments (no Access edge
    stripping headers) would otherwise allow ownership spoofing.

    When CF Access is the identity plane, mint an app JWT (or verify Access
    JWT via ``CF_Authorization``) so ownership uses the same verified subject.
    """
    if config.jwt.mode in {"none", "dev"}:  # ty:ignore[unresolved-attribute]
        return None
    token = extract_bearer_or_cookie(request)
    if not token:
        return None
    try:
        decoded: object = await decode_jwt(token, config.jwt)  # ty:ignore[invalid-await, unresolved-attribute]
        return decoded
    except JwtValidationError:
        return None


def _read_header(request: object, *names: str) -> str:
    """Read the first non-empty value of ``names`` from ``request.headers``."""
    try:
        headers = request.headers  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
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

    Identity is taken only from a verified app JWT (bearer/cookie). Unsigned
    CF Access email headers are ignored — same threat model as
    :func:`_decode_jwt_principal`.

    Returns ``"anonymous"`` when no verified JWT subject is available.
    """
    token = extract_bearer_or_cookie(request)
    if not token:
        return "anonymous"
    try:
        principal = await decode_jwt(token, config.jwt)  # ty:ignore[invalid-await, unresolved-attribute]
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
