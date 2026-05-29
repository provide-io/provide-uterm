#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for dev-auth safety guardrails.

Validates:
- require_jwt_in_production blocks dev/none mode at startup
- X-Auth-Mode response header is set in dev/none mode
- X-Auth-Mode header is absent in jwt/header modes
- Backward compatibility: dev mode still works on loopback without the flag
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from provide.uterm.server.app import _validate_auth_config, create_server_app
from provide.uterm.server.models import AuthConfig, SecurityConfig, ServerBindConfig, ServerConfig
from provide.uterm.server.security import SecurityHeadersMiddleware

# ---------------------------------------------------------------------------
# require_jwt_in_production guardrail
# ---------------------------------------------------------------------------


class TestPlaceholderCredentialGuardrails:
    """Production-like auth configs must not accept known placeholder credentials."""

    def test_jwt_mode_rejects_placeholder_key_and_worker_token_when_flag_set(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem="key",
                jwt_algorithms=["HS256"],
                worker_bearer_token="token",
            )
        )

        with pytest.raises(ValueError, match="placeholder"):
            _validate_auth_config(config)

    def test_jwt_mode_rejects_placeholder_credentials_on_non_loopback_bind(self) -> None:
        config = ServerConfig(
            server=ServerBindConfig(host="0.0.0.0"),
            auth=AuthConfig(
                mode="jwt",
                jwt_public_key_pem="REPLACE_WITH_REAL_PUBLIC_KEY",
                jwt_algorithms=["HS256"],
                worker_bearer_token="REPLACE_WITH_RUNTIME_WORKER_JWT",
            ),
        )

        with pytest.raises(ValueError, match="placeholder"):
            _validate_auth_config(config)

    def test_real_hs256_test_secrets_are_allowed_when_flag_set(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem="uterm-test-hs256-secret-32-byte-minimum",
                jwt_algorithms=["HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )

        _validate_auth_config(config)

    def test_header_mode_rejects_placeholder_worker_token_after_acknowledgement(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="header",
                require_jwt_in_production=True,
                header_mode_acknowledged=True,
                worker_bearer_token="token",
            )
        )

        with pytest.raises(ValueError, match="placeholder"):
            _validate_auth_config(config)

    def test_header_mode_still_requires_acknowledgement_before_worker_token_validation(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="header",
                require_jwt_in_production=True,
                header_mode_acknowledged=False,
                worker_bearer_token="token",
            )
        )

        with pytest.raises(ValueError, match="header_mode_acknowledged"):
            _validate_auth_config(config)

    @pytest.mark.parametrize("mode", ["dev", "none"])
    def test_removed_legacy_modes_are_rejected(self, mode: str) -> None:
        config = ServerConfig(auth=AuthConfig(mode=mode))

        with pytest.raises(ValueError, match="removed for security reasons"):
            _validate_auth_config(config)


class TestLowEntropyCredentialGuardrails:
    """Production-like auth configs must reject short bearer tokens / HMAC secrets.

    These are stricter than the placeholder list — a 'short but non-keyword'
    string like 'abc12345' would slip past the placeholder check but fail
    the 32-char minimum.
    """

    def test_short_worker_bearer_token_rejected(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem="uterm-test-hs256-secret-32-byte-minimum",
                jwt_algorithms=["HS256"],
                worker_bearer_token="abc12345",  # 8 chars
            )
        )
        with pytest.raises(ValueError, match="32 characters"):
            _validate_auth_config(config)

    def test_short_hmac_secret_rejected(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem="abc123",  # 6 chars, not a known placeholder
                jwt_algorithms=["HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        with pytest.raises(ValueError, match="HS256"):
            _validate_auth_config(config)

    def test_pem_public_key_passes_entropy_check(self) -> None:
        """PEM-encoded asymmetric keys are long by construction; the
        HMAC-secret entropy check must not fire on them."""
        pem = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----"
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem=pem,
                jwt_algorithms=["RS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        _validate_auth_config(config)

    def test_short_hmac_secret_skipped_when_algorithm_is_asymmetric(self) -> None:
        """A short string under RS256 isn't an HMAC secret — the entropy
        check shouldn't fire (the caller is expected to provide a PEM)."""
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem="short-key-content",
                jwt_algorithms=["RS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        # Does not raise — entropy check is HS*-specific.
        _validate_auth_config(config)

    def test_loopback_skips_entropy_check(self) -> None:
        """Backward compatibility: weak credentials on loopback are still
        permitted (production_like is false there)."""
        config = ServerConfig(
            server=ServerBindConfig(host="127.0.0.1"),
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=False,
                jwt_public_key_pem="abc123",
                jwt_algorithms=["HS256"],
                worker_bearer_token="abc12345",
            ),
        )
        _validate_auth_config(config)


# ---------------------------------------------------------------------------
# X-Auth-Mode response header
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """Create a minimal app with SecurityHeadersMiddleware for header testing."""
    app = FastAPI()
    config = SecurityConfig(mode="dev")
    app.add_middleware(SecurityHeadersMiddleware, config=config)

    @app.get("/test")
    def test_endpoint() -> dict[str, bool]:
        return {"ok": True}

    return app


class TestXAuthModeHeader:
    """X-Auth-Mode header was removed with dev/none mode; never emitted now."""

    def test_no_x_auth_mode_header(self) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/test")
        assert "X-Auth-Mode" not in resp.headers


class TestXAuthModeIntegrationWithApp:
    """Full create_server_app integration: verify X-Auth-Mode on real app."""

    def test_jwt_mode_app_no_header(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                jwt_public_key_pem="key",
                jwt_algorithms=["HS256"],
                worker_bearer_token="token",
            ),
        )
        app = create_server_app(config, api_only=True)
        with TestClient(app) as client:
            # JWT mode returns 401 for unauthenticated, but header check still works
            resp = client.get("/api/health")
            assert "X-Auth-Mode" not in resp.headers


class TestDevTokenMode:
    """dev_token mode validates and reaches the JWT branch."""

    def test_dev_token_on_loopback_validates(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        _validate_auth_config(config)
        # Validator mutates the config in-place into jwt mode.
        assert config.auth.mode == "jwt"

    def test_dev_token_app_can_be_created(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        app = create_server_app(config, api_only=True)
        assert app is not None


# ---------------------------------------------------------------------------
# JWT algorithm-confusion startup guard (ALG)
# ---------------------------------------------------------------------------

# A syntactically valid PEM public-key armour marker — enough for the guard,
# which only inspects the leading "-----BEGIN" marker.
_FAKE_PUBLIC_KEY_PEM = "-----BEGIN PUBLIC KEY-----\nMFkw...not-a-real-key...\n-----END PUBLIC KEY-----"
# A long non-PEM HMAC shared secret (legitimate for a pure HS256 config).
_VALID_HMAC_SECRET = "uterm-test-hs256-secret-32-byte-minimum"  # pragma: allowlist secret


class TestJwtAlgorithmConfusionGuard:
    """Reject configs that mix HMAC (HS*) with asymmetric algorithms or a public key.

    If both HS256 and an RSA/EC public key are accepted, an attacker forges an
    HS256 token using the public-key bytes as the HMAC secret. The guard must
    fire loudly at config-load time, never per-request.
    """

    def test_rejects_hmac_mixed_with_asymmetric_algorithm(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                jwt_public_key_pem=_FAKE_PUBLIC_KEY_PEM,
                jwt_algorithms=["RS256", "HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        with pytest.raises(ValueError, match="algorithm"):
            _validate_auth_config(config)

    def test_rejects_hmac_with_public_key_pem(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                jwt_public_key_pem=_FAKE_PUBLIC_KEY_PEM,
                jwt_algorithms=["HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        with pytest.raises(ValueError, match="algorithm"):
            _validate_auth_config(config)

    def test_rejects_hmac_with_jwks_url(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
                jwt_algorithms=["HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        with pytest.raises(ValueError, match="algorithm"):
            _validate_auth_config(config)

    def test_allows_pure_asymmetric_config(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                jwt_public_key_pem=_FAKE_PUBLIC_KEY_PEM,
                jwt_algorithms=["RS256", "ES256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        _validate_auth_config(config)  # must not raise

    def test_allows_pure_hmac_config_with_shared_secret(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                jwt_public_key_pem=_VALID_HMAC_SECRET,
                jwt_algorithms=["HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            )
        )
        _validate_auth_config(config)  # must not raise
