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


def _resolve_signing_key(token: str, config: JwtConfig) -> Any:
    """Return the signing key to use for *token* based on *config*.

    Supports both static PEM keys and JWKS URLs (key rotation / RS256 / ES256).
    """
    if config.jwks_url:
        client = jwt.PyJWKClient(config.jwks_url)
        return client.get_signing_key_from_jwt(token).key
    if config.public_key_pem:
        return config.public_key_pem
    raise JwtValidationError("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode")


def decode_jwt(token: str, config: JwtConfig) -> Principal:
    if config.mode in {"none", "dev"}:
        return Principal(subject_id="dev", roles=("admin",))
    if not config.public_key_pem and not config.jwks_url:
        raise JwtValidationError("missing jwt public key")

    try:
        key = _resolve_signing_key(token, config)
    except JwtValidationError:
        raise
    except Exception as exc:
        raise JwtValidationError(f"failed to resolve signing key: {exc}") from exc

    options = {
        "verify_aud": bool(config.audience),
        "verify_iss": bool(config.issuer),
        "require": ["sub", "exp", "iat", "nbf"],
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

    raw_roles = claims.get("roles", [])
    if isinstance(raw_roles, str):
        roles = tuple(part.strip() for part in raw_roles.split(",") if part.strip())
    elif isinstance(raw_roles, list):
        roles = tuple(str(role) for role in raw_roles)
    else:
        roles = ()
    return Principal(subject_id=sub, roles=roles)


def resolve_role(principal: Principal) -> str:
    role_set = set(principal.roles)
    if "admin" in role_set:
        return "admin"
    if "operator" in role_set:
        return "operator"
    return "viewer"
