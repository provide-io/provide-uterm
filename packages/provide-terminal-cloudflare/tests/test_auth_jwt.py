from __future__ import annotations

import time

import jwt
import pytest
from provide_terminal_cloudflare.auth.jwt import JwtValidationError, decode_jwt, resolve_role
from provide_terminal_cloudflare.config import JwtConfig


def test_decode_jwt_hs256_ok() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "roles": ["operator"], "iat": now, "nbf": now, "exp": now + 600},
        "secret",
        algorithm="HS256",
    )
    principal = decode_jwt(
        token,
        JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",), issuer=None, audience=None),
    )
    assert principal.subject_id == "u1"
    assert resolve_role(principal) == "operator"


def test_decode_jwt_missing_sub() -> None:
    token = jwt.encode({"roles": ["admin"]}, "secret", algorithm="HS256")
    with pytest.raises(JwtValidationError):
        decode_jwt(token, JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",)))
