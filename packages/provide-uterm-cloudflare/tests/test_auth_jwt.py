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
        WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"

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
        WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"

    cfg = CloudflareConfig.from_env(_FakeEnv())
    assert cfg.jwt.jwt_role_map == {"engineering": "admin", "ops": "operator"}


def test_config_jwt_role_map_invalid_json_ignored() -> None:
    """Invalid JWT_ROLE_MAP JSON is silently ignored (empty map)."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    class _FakeEnv:
        AUTH_MODE = "jwt"
        JWT_PUBLIC_KEY_PEM = "pem"
        JWT_ROLE_MAP = "not-valid-json{"
        WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"

    cfg = CloudflareConfig.from_env(_FakeEnv())
    assert cfg.jwt.jwt_role_map == {}


# ---------------------------------------------------------------------------
# CB-3: dev/none auth bypass must be impossible to configure or invoke.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["dev", "none", "DEV", "None"])
def test_from_env_rejects_dev_and_none_modes_in_all_environments(mode: str) -> None:
    """AUTH_MODE=dev/none must raise regardless of ENVIRONMENT (no internet-facing admin bypass)."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    for environment in ("development", "production", "staging"):
        with pytest.raises(ValueError, match="AUTH_MODE"):
            CloudflareConfig.from_env(
                {
                    "ENVIRONMENT": environment,
                    "AUTH_MODE": mode,
                    "JWT_PUBLIC_KEY_PEM": "pem",
                    "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
                }
            )


async def test_decode_jwt_has_no_dev_bypass() -> None:
    """decode_jwt must never mint an admin principal without a verified token."""
    cfg = JwtConfig(mode="dev", public_key_pem="any")
    with pytest.raises(JwtValidationError):
        await decode_jwt("not.a.valid.token", cfg)


async def test_decode_jwt_has_no_none_bypass() -> None:
    """decode_jwt must never mint an admin principal in 'none' mode either."""
    cfg = JwtConfig(mode="none", public_key_pem="any")
    with pytest.raises(JwtValidationError):
        await decode_jwt("not.a.valid.token", cfg)


# ---------------------------------------------------------------------------
# CF-svc: CF Access service tokens only get admin when explicitly enabled.
# ---------------------------------------------------------------------------

_SVC_SECRET = "uterm-test-secret-32-byte-minimum-key"


def _svc_token(**claims: object) -> str:
    now = int(time.time())
    payload: dict = {"exp": now + 600}
    payload.update(claims)
    return jwt.encode(payload, _SVC_SECRET, algorithm="HS256")


async def test_empty_sub_service_token_not_admin_by_default() -> None:
    """A service-token-shaped JWT must NOT be auto-granted admin unless opted in.

    Default config does not enable service-token admin, so common_name becomes
    the subject but the principal gets only the default role, never admin.
    """
    token = _svc_token(sub="", common_name="acme-client-id")
    cfg = JwtConfig(mode="jwt", public_key_pem=_SVC_SECRET, algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert principal.subject_id == "acme-client-id"
    assert "admin" not in principal.roles
    assert resolve_role(principal) == "viewer"


async def test_service_token_admin_requires_opt_in_and_common_name() -> None:
    """With service-token admin enabled, a token carrying common_name gets admin."""
    token = _svc_token(sub="", common_name="acme-client-id")
    cfg = JwtConfig(
        mode="jwt",
        public_key_pem=_SVC_SECRET,
        algorithms=("HS256",),
        jwt_service_token_admin=True,
    )
    principal = await decode_jwt(token, cfg)
    assert principal.subject_id == "acme-client-id"
    assert principal.roles == ("admin",)


async def test_service_token_admin_opt_in_rejects_human_email_token() -> None:
    """Even with service-token admin enabled, a token carrying a human email claim
    is treated as a user (never silently elevated to a service-token admin)."""
    token = _svc_token(sub="", common_name="acme-client-id", email="person@example.com")
    cfg = JwtConfig(
        mode="jwt",
        public_key_pem=_SVC_SECRET,
        algorithms=("HS256",),
        jwt_service_token_admin=True,
    )
    principal = await decode_jwt(token, cfg)
    assert "admin" not in principal.roles


def test_config_reads_jwt_service_token_admin_from_env() -> None:
    """JWT_SERVICE_TOKEN_ADMIN env var wires JwtConfig.jwt_service_token_admin."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    cfg = CloudflareConfig.from_env(
        {
            "AUTH_MODE": "jwt",
            "JWT_PUBLIC_KEY_PEM": "pem",
            "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
            "JWT_SERVICE_TOKEN_ADMIN": "1",
        }
    )
    assert cfg.jwt.jwt_service_token_admin is True


def test_config_jwt_service_token_admin_defaults_false() -> None:
    """Service-token admin is opt-in: defaults to False (fail closed)."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    cfg = CloudflareConfig.from_env(
        {"AUTH_MODE": "jwt", "JWT_PUBLIC_KEY_PEM": "pem", "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz"}
    )
    assert cfg.jwt.jwt_service_token_admin is False


# ---------------------------------------------------------------------------
# ALG: JWT algorithm-confusion startup guard.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "algs",
    ["RS256,HS256", "HS256", "ES256,HS384", "PS256,HS512"],
)
def test_jwt_config_rejects_hs_mixed_with_pem_key(algs: str) -> None:
    """HMAC algorithms combined with a PEM public key are an algorithm-confusion risk."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    with pytest.raises(ValueError, match="algorithm"):
        CloudflareConfig.from_env(
            {
                "AUTH_MODE": "jwt",
                "JWT_ALGORITHMS": algs,
                "JWT_PUBLIC_KEY_PEM": "-----BEGIN PUBLIC KEY-----\nXXXX\n-----END PUBLIC KEY-----",
                "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
            }
        )


def test_jwt_config_rejects_hs_mixed_with_jwks_url() -> None:
    """HMAC algorithms combined with a JWKS URL are an algorithm-confusion risk."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    with pytest.raises(ValueError, match="algorithm"):
        CloudflareConfig.from_env(
            {
                "AUTH_MODE": "jwt",
                "JWT_ALGORITHMS": "RS256,HS256",
                "JWT_JWKS_URL": "https://idp.example.com/.well-known/jwks.json",
                "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
            }
        )


def test_jwt_config_rejects_hs_combined_with_asymmetric_alg() -> None:
    """Mixing HMAC and asymmetric algorithms is rejected even without a key present."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    with pytest.raises(ValueError, match="algorithm"):
        CloudflareConfig.from_env(
            {
                "AUTH_MODE": "jwt",
                "JWT_ALGORITHMS": "RS256,HS256",
                "JWT_PUBLIC_KEY_PEM": "k",
                "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
            }
        )


def test_jwt_config_allows_pure_asymmetric_with_key() -> None:
    """Asymmetric-only algorithms with a PEM key are fine."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    cfg = CloudflareConfig.from_env(
        {
            "AUTH_MODE": "jwt",
            "JWT_ALGORITHMS": "RS256,ES256",
            "JWT_PUBLIC_KEY_PEM": "k",
            "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
        }
    )
    assert cfg.jwt.algorithms == ("RS256", "ES256")


def test_jwt_config_allows_hs_without_asymmetric_key() -> None:
    """HMAC-only algorithms without any asymmetric key/JWKS are allowed.

    (The shared secret is supplied via JWT_PUBLIC_KEY_PEM acting as the HMAC key;
    the guard only fires when HMAC is mixed with asymmetric algs or a JWKS URL.)
    """
    from provide.uterm.cloudflare.config import CloudflareConfig

    cfg = CloudflareConfig.from_env(
        {
            "AUTH_MODE": "jwt",
            "JWT_ALGORITHMS": "HS256",
            "JWT_PUBLIC_KEY_PEM": "shared-secret-32-bytes-minimum-key!",
            "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
        }
    )
    assert cfg.jwt.algorithms == ("HS256",)


def test_jwt_config_allows_hs_with_no_key_material() -> None:
    """HMAC algorithm with neither a PEM key nor a JWKS URL passes the guard."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    cfg = CloudflareConfig.from_env(
        {"AUTH_MODE": "jwt", "JWT_ALGORITHMS": "HS256", "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz"}
    )
    assert cfg.jwt.public_key_pem is None
    assert cfg.jwt.jwks_url is None
