from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jwt
from jwt import InvalidTokenError

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
    """Fetch JWKS data using the runtime's async HTTP client.

    In Cloudflare Workers, uses the native ``js.fetch`` (async, non-blocking).
    Outside CF (tests / local dev), falls back to ``urllib`` as a last resort.
    """
    try:
        from js import fetch as _js_fetch  # type: ignore[import-not-found]  # CF Workers native async fetch

        response = await _js_fetch(url)
        return (await response.json()).to_py()  # type: ignore[no-any-return]
    except ImportError:
        pass
    import json
    import urllib.request

    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return json.loads(resp.read())  # type: ignore[no-any-return]


async def _resolve_signing_key(token: str, config: JwtConfig) -> Any:
    """Return the signing key to use for *token* based on *config*.

    Supports both static PEM keys and JWKS URLs (key rotation / RS256 / ES256).
    Uses async HTTP for JWKS so the V8 isolate is never blocked.
    """
    if config.jwks_url:
        jwks_data = await _fetch_jwks(config.jwks_url)
        jwks = jwt.PyJWKSet.from_dict(jwks_data)
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
