from __future__ import annotations

import time

import jwt
import pytest
from provide.uterm.cloudflare.auth.jwt import JwtValidationError, decode_jwt, resolve_role
from provide.uterm.cloudflare.config import JwtConfig


async def test_decode_jwt_hs256_ok() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "roles": ["operator"], "iat": now, "nbf": now, "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    principal = await decode_jwt(
        token,
        JwtConfig(
            mode="jwt",
            public_key_pem="uterm-test-secret-32-byte-minimum-key",
            algorithms=("HS256",),
            issuer=None,
            audience=None,
        ),
    )
    assert principal.subject_id == "u1"
    assert resolve_role(principal) == "operator"


async def test_decode_jwt_missing_sub() -> None:
    token = jwt.encode({"roles": ["admin"]}, "uterm-test-secret-32-byte-minimum-key", algorithm="HS256")
    with pytest.raises(JwtValidationError):
        await decode_jwt(
            token, JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
        )


async def test_cf_access_style_jwt_no_roles_defaults_to_viewer() -> None:
    """CF Access JWTs have no roles claim; default role should be viewer."""
    now = int(time.time())
    # CF Access JWTs: sub=email, aud=list, no roles claim
    token = jwt.encode(
        {
            "sub": "user@example.com",
            "aud": ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"],
            "iss": "https://myteam.cloudflareaccess.com",
            "iat": now,
            "exp": now + 600,
            "email": "user@example.com",
        },
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        issuer=None,
        audience=None,
        jwt_default_role="viewer",
    )
    principal = await decode_jwt(token, config)
    assert principal.subject_id == "user@example.com"
    assert resolve_role(principal) == "viewer"


async def test_cf_access_style_jwt_default_role_operator() -> None:
    """JWT_DEFAULT_ROLE=operator grants all CF Access users operator access."""
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user@example.com",
            "aud": ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"],
            "iat": now,
            "exp": now + 600,
        },
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        issuer=None,
        audience=None,
        jwt_default_role="operator",
    )
    principal = await decode_jwt(token, config)
    assert resolve_role(principal) == "operator"


async def test_cf_access_default_role_not_applied_when_roles_present() -> None:
    """Default role is ignored when the JWT already has roles."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "roles": ["admin"], "iat": now, "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_default_role="viewer",
    )
    principal = await decode_jwt(token, config)
    assert resolve_role(principal) == "admin"


def test_config_reads_jwt_default_role_from_env() -> None:
    """JWT_DEFAULT_ROLE env var is wired to JwtConfig.jwt_default_role."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    class _FakeEnv:
        AUTH_MODE = "jwt"
        JWT_PUBLIC_KEY_PEM = "pem"
        JWT_DEFAULT_ROLE = "operator"
        WORKER_BEARER_TOKEN = "t"

    cfg = CloudflareConfig.from_env(_FakeEnv())
    assert cfg.jwt.jwt_default_role == "operator"


async def test_role_map_translates_group_names() -> None:
    """JWT_ROLE_MAP maps arbitrary group names to terminal roles."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "groups": ["engineering", "devs"], "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_roles_claim="groups",
        jwt_role_map={"engineering": "admin", "devs": "operator"},
    )
    principal = await decode_jwt(token, config)
    assert resolve_role(principal) == "admin"


async def test_role_map_unknown_groups_pass_through() -> None:
    """Group names not in jwt_role_map are kept as-is (may not match any terminal role)."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "groups": ["unknown-group"], "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_roles_claim="groups",
        jwt_role_map={"engineering": "admin"},
    )
    principal = await decode_jwt(token, config)
    # "unknown-group" not in map → passes through → resolve_role falls back to viewer
    assert resolve_role(principal) == "viewer"


async def test_role_map_partial_match_picks_highest() -> None:
    """When a user has multiple groups, resolve_role picks the highest-privilege role."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "groups": ["ops", "everyone"], "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    config = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_roles_claim="groups",
        jwt_role_map={"ops": "operator", "everyone": "viewer"},
    )
    principal = await decode_jwt(token, config)
    assert resolve_role(principal) == "operator"


def test_config_reads_jwt_role_map_from_env() -> None:
    """JWT_ROLE_MAP env var is parsed as JSON and wired to JwtConfig.jwt_role_map."""
    import json

    from provide.uterm.cloudflare.config import CloudflareConfig

    class _FakeEnv:
        AUTH_MODE = "jwt"
        JWT_PUBLIC_KEY_PEM = "pem"
        JWT_ROLE_MAP = json.dumps({"engineering": "admin", "ops": "operator"})
        WORKER_BEARER_TOKEN = "t"

    cfg = CloudflareConfig.from_env(_FakeEnv())
    assert cfg.jwt.jwt_role_map == {"engineering": "admin", "ops": "operator"}


def test_config_jwt_role_map_invalid_json_ignored() -> None:
    """Invalid JWT_ROLE_MAP JSON is silently ignored (empty map)."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    class _FakeEnv:
        AUTH_MODE = "jwt"
        JWT_PUBLIC_KEY_PEM = "pem"
        JWT_ROLE_MAP = "not-valid-json{"
        WORKER_BEARER_TOKEN = "t"

    cfg = CloudflareConfig.from_env(_FakeEnv())
    assert cfg.jwt.jwt_role_map == {}
