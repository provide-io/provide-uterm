from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from jwt import InvalidTokenError

try:
    from provide_terminal_cloudflare.config import JwtConfig
except Exception:
    from config import JwtConfig


class JwtValidationError(ValueError):
    pass


@dataclass(slots=True)
class Principal:
    subject_id: str
    roles: tuple[str, ...]


def decode_jwt(token: str, config: JwtConfig) -> Principal:
    if config.mode in {"none", "dev"}:
        return Principal(subject_id="dev", roles=("admin",))
    if not config.public_key_pem:
        raise JwtValidationError("missing jwt public key")

    options = {"verify_aud": bool(config.audience), "verify_iss": bool(config.issuer)}
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            config.public_key_pem,
            algorithms=list(config.algorithms),
            issuer=config.issuer,
            audience=config.audience,
            options=options,
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
