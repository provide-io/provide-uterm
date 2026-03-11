from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jwt
from jwt import InvalidTokenError

# Module-level JWKS cache: url → (fetched_at, jwks_dict).
# Avoids a network round-trip on every authenticated request within the same
# V8 isolate lifetime.  The cache is per-isolate (not shared across requests
# in different isolates), so the TTL only matters within a single long-lived isolate.
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_CACHE_TTL_S: float = 60.0

if TYPE_CHECKING:
    from provide_terminal_cloudflare.config import JwtConfig
else:
    JwtConfig = Any


class JwtValidationError(ValueError):
    pass


@dataclass(slots=True)
class Principal:
    subject_id: str
    roles: tuple[str, ...]


async def _fetch_jwks(url: str) -> dict[str, Any]:
    """Fetch JWKS data, with a per-isolate in-memory TTL cache.

    In Cloudflare Workers, uses the native ``js.fetch`` (async, non-blocking).
    Outside CF (tests / local dev), falls back to ``urllib`` as a last resort.
    The cache avoids a network round-trip on every authenticated request within
    the same isolate lifetime.
    """
    now = time.time()
    cached = _JWKS_CACHE.get(url)
    if cached is not None:
        fetched_at, jwks_dict = cached
        if now - fetched_at < _JWKS_CACHE_TTL_S:
            return jwks_dict

    try:
        from js import (
            fetch as _js_fetch,  # type: ignore[import-not-found]  # CF Workers native async fetch  # pragma: no cover
        )

        response = await _js_fetch(url)  # pragma: no cover
        result: dict[str, Any] = (await response.json()).to_py()  # pragma: no cover
    except ImportError:
        import json
        import urllib.request

        _req = urllib.request.Request(url, headers={"User-Agent": "provide-terminal/1.0"})  # noqa: S310
        with urllib.request.urlopen(_req) as resp:  # noqa: S310
            result = json.loads(resp.read())

    _JWKS_CACHE[url] = (now, result)
    return result


async def _resolve_signing_key(token: str, config: JwtConfig) -> Any:
    """Return the signing key to use for *token* based on *config*.

    Supports both static PEM keys and JWKS URLs (key rotation / RS256 / ES256).
    Uses async HTTP for JWKS so the V8 isolate is never blocked.
    """
    if config.jwks_url:
        jwks_data = await _fetch_jwks(config.jwks_url)
        try:
            jwks = jwt.PyJWKSet.from_dict(jwks_data)
        except Exception as exc:
            raise JwtValidationError(f"no matching key found in JWKS: {exc}") from exc
        headers = jwt.get_unverified_header(token)
        kid = headers.get("kid")
        alg = headers.get("alg")
        for key in jwks.keys:
            if kid is not None:
                if key.key_id == kid:
                    return key.key
            else:
                # No kid: match by algorithm to avoid returning an incompatible key.
                key_alg = getattr(key, "algorithm_name", None)
                if alg is None or key_alg is None or key_alg == alg:
                    return key.key
        raise JwtValidationError("no matching key found in JWKS")
    if config.public_key_pem:
        return config.public_key_pem
    raise JwtValidationError("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode")


async def decode_jwt(token: str, config: JwtConfig) -> Principal:
    if config.mode in {"none", "dev"}:
        return Principal(subject_id="dev", roles=("admin",))
    if not config.public_key_pem and not config.jwks_url:
        raise JwtValidationError("missing jwt public key")

    try:
        key = await _resolve_signing_key(token, config)
    except JwtValidationError:
        raise
    except Exception as exc:
        raise JwtValidationError(f"failed to resolve signing key: {exc}") from exc

    options = {
        "verify_aud": bool(config.audience),
        "verify_iss": bool(config.issuer),
        # Match FastAPI: only sub+exp required so Auth0/Google/Azure AD tokens
        # that omit iat/nbf are accepted without config changes.
        "require": ["sub", "exp"],
    }
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=list(config.algorithms),
            issuer=config.issuer,
            audience=config.audience,
            options=options,
            leeway=max(0, int(config.clock_skew_seconds)),
        )
    except InvalidTokenError as exc:
        raise JwtValidationError(str(exc)) from exc

    sub = str(claims.get("sub") or "")
    if not sub:
        raise JwtValidationError("missing sub")

    roles_claim = getattr(config, "jwt_roles_claim", "roles")
    scopes_claim = getattr(config, "jwt_scopes_claim", "scope")

    raw_roles = claims.get(roles_claim, [])
    if isinstance(raw_roles, str):
        roles = tuple(part.strip() for part in raw_roles.split(",") if part.strip())
    elif isinstance(raw_roles, list):
        roles = tuple(str(role) for role in raw_roles)
    else:
        roles = ()

    # Fall back to scope claim when no roles are present (e.g. Auth0 machine-to-machine tokens).
    if not roles:
        raw_scope = claims.get(scopes_claim, "")
        if isinstance(raw_scope, str) and raw_scope:
            roles = tuple(part.strip() for part in raw_scope.split() if part.strip())

    # Apply group→role mapping when configured (e.g. JWT_ROLE_MAP={"engineering":"admin"}).
    # Each role value is looked up in the map; unrecognised values pass through unchanged.
    role_map: dict[str, str] = getattr(config, "jwt_role_map", {}) or {}
    if role_map:
        roles = tuple(role_map.get(r, r) for r in roles)

    # If still no roles (e.g. CF Access JWTs which omit roles by default),
    # assign the configured default role so operators don't end up as viewers.
    if not roles:
        default_role = getattr(config, "jwt_default_role", "viewer") or "viewer"
        roles = (default_role,)

    return Principal(subject_id=sub, roles=roles)


def resolve_role(principal: Principal) -> str:
    role_set = set(principal.roles)
    if "admin" in role_set:
        return "admin"
    if "operator" in role_set:
        return "operator"
    return "viewer"


def extract_bearer_or_cookie(request: object) -> str | None:
    """Extract a JWT from the Authorization: Bearer header or CF_Authorization cookie.

    Used by both the Default worker (entry.py) and the SessionRuntime DO
    (session_runtime.py).  Browser WebSockets cannot send custom headers, so
    the CF_Authorization cookie is the only auth mechanism available for WS
    upgrade requests protected by Cloudflare Access.
    """
    try:
        auth_header = str(request.headers.get("Authorization") or "")  # type: ignore[attr-defined]
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if token:
                return token
    except Exception:  # noqa: S110
        pass
    try:
        cookie_header = str(request.headers.get("Cookie") or "")  # type: ignore[attr-defined]
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == "CF_Authorization" and value.strip():
                return value.strip()
    except Exception:  # noqa: S110
        pass
    return None
