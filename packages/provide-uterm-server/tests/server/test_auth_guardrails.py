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


class TestRequireJwtInProduction:
    """The require_jwt_in_production flag must block dev/none auth modes."""

    def test_dev_mode_blocked_when_flag_set(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="dev", require_jwt_in_production=True),
        )
        with pytest.raises(RuntimeError, match="require_jwt_in_production"):
            _validate_auth_config(config)

    def test_none_mode_blocked_when_flag_set(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="none", require_jwt_in_production=True),
        )
        with pytest.raises(RuntimeError, match="require_jwt_in_production"):
            _validate_auth_config(config)

    def test_dev_mode_allowed_when_flag_false(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="dev", require_jwt_in_production=False),
        )
        _validate_auth_config(config)

    def test_none_mode_allowed_when_flag_false(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="none", require_jwt_in_production=False),
        )
        _validate_auth_config(config)

    def test_jwt_mode_unaffected_by_flag(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="jwt",
                require_jwt_in_production=True,
                jwt_public_key_pem="uterm-test-hs256-secret-32-byte-minimum",
                jwt_algorithms=["HS256"],
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            ),
        )
        _validate_auth_config(config)

    def test_header_mode_unaffected_by_flag(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="header",
                require_jwt_in_production=True,
                header_mode_acknowledged=True,
                worker_bearer_token="uterm-test-worker-bearer-value-32-bytes",
            ),
        )
        _validate_auth_config(config)

    def test_flag_defaults_to_false(self) -> None:
        auth = AuthConfig(mode="dev")
        assert auth.require_jwt_in_production is False

    def test_flag_checked_before_loopback_check(self) -> None:
        """Flag must fire even on loopback, preventing any dev-mode startup."""
        config = ServerConfig(
            server=ServerBindConfig(host="127.0.0.1"),
            auth=AuthConfig(mode="dev", require_jwt_in_production=True),
        )
        with pytest.raises(RuntimeError, match="require_jwt_in_production"):
            _validate_auth_config(config)

    def test_error_message_includes_mode(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="dev", require_jwt_in_production=True),
        )
        with pytest.raises(RuntimeError, match="auth.mode='dev'"):
            _validate_auth_config(config)

    def test_error_message_includes_none_mode(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="none", require_jwt_in_production=True),
        )
        with pytest.raises(RuntimeError, match="auth.mode='none'"):
            _validate_auth_config(config)


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


# ---------------------------------------------------------------------------
# X-Auth-Mode response header
# ---------------------------------------------------------------------------


def _make_app(auth_mode: str | None = None) -> FastAPI:
    """Create a minimal app with SecurityHeadersMiddleware for header testing."""
    app = FastAPI()
    config = SecurityConfig(mode="dev")
    app.add_middleware(SecurityHeadersMiddleware, config=config, auth_mode=auth_mode)

    @app.get("/test")
    def test_endpoint() -> dict[str, bool]:
        return {"ok": True}

    return app


class TestXAuthModeHeader:
    """X-Auth-Mode header must appear in dev/none mode and be absent otherwise."""

    def test_dev_mode_sets_header(self) -> None:
        app = _make_app(auth_mode="dev")
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers.get("X-Auth-Mode") == "dev"

    def test_none_mode_sets_header(self) -> None:
        app = _make_app(auth_mode="none")
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers.get("X-Auth-Mode") == "none"

    def test_jwt_mode_no_header(self) -> None:
        app = _make_app(auth_mode="jwt")
        client = TestClient(app)
        resp = client.get("/test")
        assert "X-Auth-Mode" not in resp.headers

    def test_header_mode_no_header(self) -> None:
        app = _make_app(auth_mode="header")
        client = TestClient(app)
        resp = client.get("/test")
        assert "X-Auth-Mode" not in resp.headers

    def test_none_auth_mode_param_no_header(self) -> None:
        """When auth_mode kwarg is None, no X-Auth-Mode header is added."""
        app = _make_app(auth_mode=None)
        client = TestClient(app)
        resp = client.get("/test")
        assert "X-Auth-Mode" not in resp.headers


class TestXAuthModeIntegrationWithApp:
    """Full create_server_app integration: verify X-Auth-Mode on real app."""

    def test_dev_mode_app_sets_header(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev"))
        app = create_server_app(config, api_only=True)
        with TestClient(app) as client:
            resp = client.get("/api/health")
            assert resp.headers.get("X-Auth-Mode") == "dev"

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


# ---------------------------------------------------------------------------
# Backward compatibility: dev mode still works on loopback
# ---------------------------------------------------------------------------


class TestDevModeBackwardCompatibility:
    """Dev mode must still function on loopback without require_jwt_in_production."""

    def test_dev_mode_on_loopback_still_works(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev"))
        _validate_auth_config(config)

    def test_none_mode_on_loopback_still_works(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="none"))
        _validate_auth_config(config)

    def test_dev_mode_create_app_still_works(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev"))
        app = create_server_app(config, api_only=True)
        assert app is not None
