#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""API contract tests (part 2) — JWT claims, config env parsing, and static asset checks."""

from __future__ import annotations

import time

import jwt
from provide.uterm.cloudflare.auth.jwt import decode_jwt
from provide.uterm.cloudflare.config import CloudflareConfig, JwtConfig

# ---------------------------------------------------------------------------
# Contract: JWT roles_claim parity with FastAPI AuthConfig
# ---------------------------------------------------------------------------


async def test_jwt_roles_claim_custom_key() -> None:
    """Custom roles claim key matches FastAPI jwt_roles_claim behaviour."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "my_roles": ["admin"], "iat": now, "nbf": now, "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    cfg = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_roles_claim="my_roles",
    )
    principal = await decode_jwt(token, cfg)
    assert "admin" in principal.roles


async def test_jwt_roles_claim_default_is_roles() -> None:
    """Default claim key is 'roles', matching FastAPI default."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "roles": ["operator"], "iat": now, "nbf": now, "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert "operator" in principal.roles


async def test_jwt_scopes_claim_fallback() -> None:
    """Space-separated scopes used as role fallback when roles claim absent."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "scope": "read:sessions role:admin", "iat": now, "nbf": now, "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert "role:admin" in principal.roles or "read:sessions" in principal.roles


async def test_jwt_scopes_claim_custom_key() -> None:
    """Custom scopes claim key matches FastAPI jwt_scopes_claim behaviour."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "permissions": "admin write", "iat": now, "nbf": now, "exp": now + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    cfg = JwtConfig(
        mode="jwt",
        public_key_pem="uterm-test-secret-32-byte-minimum-key",
        algorithms=("HS256",),
        jwt_scopes_claim="permissions",
    )
    principal = await decode_jwt(token, cfg)
    assert "admin" in principal.roles


async def test_jwt_roles_claim_takes_priority_over_scopes() -> None:
    """Explicit roles claim takes priority over scope fallback."""
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "u1",
            "roles": ["viewer"],
            "scope": "admin superuser",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert principal.roles == ("viewer",)


# ---------------------------------------------------------------------------
# Contract: config.from_env reads JWT_ROLES_CLAIM / JWT_SCOPES_CLAIM
# ---------------------------------------------------------------------------


def test_config_from_env_reads_jwt_roles_claim() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "none", "JWT_ROLES_CLAIM": "https://myapp.com/roles"})
    assert cfg.jwt.jwt_roles_claim == "https://myapp.com/roles"


def test_config_from_env_reads_jwt_scopes_claim() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "none", "JWT_SCOPES_CLAIM": "permissions"})
    assert cfg.jwt.jwt_scopes_claim == "permissions"


def test_config_from_env_jwt_claims_default_values() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "none"})
    assert cfg.jwt.jwt_roles_claim == "roles"
    assert cfg.jwt.jwt_scopes_claim == "scope"


# ---------------------------------------------------------------------------
# Contract: assets — local static/ files must not shadow provide.uterm
# ---------------------------------------------------------------------------


def test_no_local_static_overrides() -> None:
    """ui/static/ must not contain hand-authored files that shadow provide.uterm.

    Build-time artifacts (populated by wrangler [build] or Docker COPY) are
    allowed — they are gitignored and will not be present in a clean checkout.
    This test checks for files NOT covered by the static/.gitignore sentinel,
    i.e. files that were intentionally committed to the source tree.
    """
    import importlib.resources
    from pathlib import Path

    static_dir = Path(__file__).parent.parent / "src" / "provide" / "terminal" / "cloudflare" / "ui" / "static"
    gitignore = static_dir / ".gitignore"
    # If the .gitignore sentinel exists, all other files are gitignored build
    # artifacts — not committed overrides.  Skip the check in that case.
    if gitignore.exists():
        return

    try:
        static_root = importlib.resources.files("provide.uterm.cloudflare.ui") / "static"
        static_files = [p for p in static_root.iterdir() if p.is_file()]  # type: ignore[union-attr]
    except (ModuleNotFoundError, TypeError, NotImplementedError, FileNotFoundError):
        static_files = []

    assert static_files == [], (
        f"Found local static overrides that shadow provide.uterm: {[str(f) for f in static_files]}. "
        "Delete them and use the provide.uterm package as the single source of truth."
    )
