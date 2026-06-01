#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the unified environment profile + security-posture self-report.

Covers:
  * ``ServerConfig.environment`` field (default/accept/reject)
  * ``compute_security_posture`` pure-function posture report
  * ``_validate_environment_profile`` production assertion
  * ``GET /api/security-posture`` endpoint (auth-gated)
  * startup posture log line
"""

from __future__ import annotations

import json
import logging
import time

import jwt as _jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.app.auth import _validate_environment_profile
from provide.uterm.server.app.posture import compute_security_posture
from provide.uterm.server.models import AuthConfig, SecurityConfig, ServerConfig

_HS256_KEY = "uterm-test-secret-32-byte-minimum-key"


def _jwt_production_config(*, roles: list[str]) -> tuple[ServerConfig, str]:
    """Return a jwt-mode production config on 0.0.0.0 and a matching token."""
    now = int(time.time())
    token = _jwt.encode(
        {
            "sub": "principal1",
            "roles": roles,
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_HS256_KEY,
        algorithm="HS256",
    )
    config = default_server_config()
    config.environment = "production"
    config.server.host = "0.0.0.0"
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_HS256_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=token,
    )
    return config, token


# ---------------------------------------------------------------------------
# 1. environment field
# ---------------------------------------------------------------------------


class TestEnvironmentField:
    def test_defaults_to_production(self) -> None:
        assert ServerConfig().environment == "production"

    def test_accepts_dev(self) -> None:
        assert ServerConfig(environment="dev").environment == "dev"

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            ServerConfig(environment="staging")


# ---------------------------------------------------------------------------
# 2. compute_security_posture
# ---------------------------------------------------------------------------


class TestComputeSecurityPosture:
    def test_default_dev_token_on_loopback(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        posture = compute_security_posture(config)
        assert posture["auth_mode"] == "dev_token"
        assert posture["is_loopback"] is True
        assert posture["environment"] == "production"
        assert "auth.mode=dev_token" in posture["dev_opt_outs"]
        # Loopback-only weakening: the deployment is unreachable remotely, so
        # secure stays True even with dev_token active.
        assert posture["secure"] is True

    def test_dev_opt_outs_sorted(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="dev_token", allow_adhoc_browser_observers=True),
        )
        posture = compute_security_posture(config)
        assert posture["dev_opt_outs"] == sorted(posture["dev_opt_outs"])

    def test_jwt_production_no_opt_outs_secure(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        # block_private default False is a connector relaxation; enable it so
        # there are truly zero weakening opt-outs for this assertion.
        config.security.block_private_connector_targets = True
        posture = compute_security_posture(config)
        assert posture["dev_opt_outs"] == []
        assert posture["is_loopback"] is False
        assert posture["secure"] is True

    def test_block_private_false_reported_as_opt_out(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        # default block_private_connector_targets=False
        posture = compute_security_posture(config)
        assert any("block_private_connector_targets" in entry for entry in posture["dev_opt_outs"])

    def test_webhook_idp_viewer_in_opt_outs_and_warnings(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token", webhook_idp_on_failure="viewer"))
        posture = compute_security_posture(config)
        assert "auth.webhook_idp_on_failure=viewer" in posture["dev_opt_outs"]
        assert any("anonymous" in w.lower() for w in posture["warnings"])

    def test_webhook_idp_deny_not_in_opt_outs(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token", webhook_idp_on_failure="deny"))
        posture = compute_security_posture(config)
        assert "auth.webhook_idp_on_failure=viewer" not in posture["dev_opt_outs"]

    def test_security_dev_mode_on_non_loopback_not_secure(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.security = SecurityConfig(
            mode="dev",
            dev_mode_acknowledged=True,
            block_private_connector_targets=True,
        )
        posture = compute_security_posture(config)
        assert "security.mode=dev" in posture["dev_opt_outs"]
        assert "security.dev_mode_acknowledged" in posture["dev_opt_outs"]
        # Weakening opt-out active on a non-loopback bind → not secure.
        assert posture["secure"] is False
        assert any("dev_token auth" in w or "HSTS" in w or "header" in w.lower() for w in posture["warnings"])

    def test_security_dev_mode_unacknowledged_omits_ack_opt_out(self) -> None:
        # security.mode=dev without dev_mode_acknowledged: mode=dev is reported,
        # but the acknowledgement opt-out is not (covers the inner-if False path).
        config = ServerConfig(
            auth=AuthConfig(mode="dev_token"),
            security=SecurityConfig(mode="dev", dev_mode_acknowledged=False),
        )
        posture = compute_security_posture(config)
        assert "security.mode=dev" in posture["dev_opt_outs"]
        assert "security.dev_mode_acknowledged" not in posture["dev_opt_outs"]

    def test_header_mode_acknowledged_reported_only_when_header_mode(self) -> None:
        # header_mode_acknowledged True but mode is dev_token → NOT active.
        config = ServerConfig(auth=AuthConfig(mode="dev_token", header_mode_acknowledged=True))
        posture = compute_security_posture(config)
        assert "auth.header_mode_acknowledged" not in posture["dev_opt_outs"]

    def test_header_mode_acknowledged_active_in_header_mode(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="header",
                header_mode_acknowledged=True,
                worker_bearer_token="test-bearer-token-32-chars-long-x",
            ),
        )
        posture = compute_security_posture(config)
        assert "auth.header_mode_acknowledged" in posture["dev_opt_outs"]

    def test_dev_mode_acknowledged_reported_only_when_dev_security_mode(self) -> None:
        # dev_mode_acknowledged True but security.mode is strict → NOT active.
        config = ServerConfig(
            auth=AuthConfig(mode="dev_token"),
            security=SecurityConfig(mode="strict", dev_mode_acknowledged=True),
        )
        posture = compute_security_posture(config)
        assert "security.dev_mode_acknowledged" not in posture["dev_opt_outs"]

    def test_allow_adhoc_browser_observers_opt_out(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token", allow_adhoc_browser_observers=True))
        posture = compute_security_posture(config)
        assert "auth.allow_adhoc_browser_observers" in posture["dev_opt_outs"]

    def test_environment_dev_is_not_secure(self) -> None:
        config = ServerConfig(environment="dev", auth=AuthConfig(mode="dev_token"))
        posture = compute_security_posture(config)
        # secure requires environment=="production".
        assert posture["secure"] is False

    def test_idp_signing_required_reflects_real_field_default_true(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        posture = compute_security_posture(config)
        # 1f/1d: the field is now real and defaults to True (secure-by-default).
        assert posture["idp_signing_required"] is True

    def test_idp_signing_required_picked_up_when_present(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        posture = compute_security_posture(config)
        assert posture["idp_signing_required"] is True

    def test_webhook_unsigned_response_in_opt_outs_and_warnings(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="dev_token",
                identity_provider="webhook",
                webhook_idp_require_signed_response=False,
            ),
        )
        posture = compute_security_posture(config)
        assert posture["idp_signing_required"] is False
        assert "auth.webhook_idp_require_signed_response=false" in posture["dev_opt_outs"]
        assert any("forged response" in w.lower() or "not signature-verified" in w.lower() for w in posture["warnings"])

    def test_webhook_unsigned_opt_out_absent_when_local_idp(self) -> None:
        # require_signed_response=False is only a posture opt-out for the webhook IdP.
        config = ServerConfig(
            auth=AuthConfig(
                mode="dev_token",
                identity_provider="local",
                webhook_idp_require_signed_response=False,
            ),
        )
        posture = compute_security_posture(config)
        assert "auth.webhook_idp_require_signed_response=false" not in posture["dev_opt_outs"]

    def test_webhook_signed_response_not_in_opt_outs(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="dev_token",
                identity_provider="webhook",
                webhook_idp_secret="uterm-test-secret-32-byte-minimum-key",  # pragma: allowlist secret
                webhook_idp_require_signed_response=True,
            ),
        )
        posture = compute_security_posture(config)
        assert "auth.webhook_idp_require_signed_response=false" not in posture["dev_opt_outs"]

    def test_webhook_unsigned_posture_is_json_serializable(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="dev_token",
                identity_provider="webhook",
                webhook_idp_require_signed_response=False,
            ),
        )
        posture = compute_security_posture(config)
        assert json.loads(json.dumps(posture)) == posture

    def test_json_serializable(self) -> None:
        config = ServerConfig(auth=AuthConfig(mode="dev_token", webhook_idp_on_failure="viewer"))
        posture = compute_security_posture(config)
        # Must round-trip cleanly through json.
        assert json.loads(json.dumps(posture)) == posture

    def test_bind_host_reported(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        posture = compute_security_posture(config)
        assert posture["bind_host"] == "0.0.0.0"

    def test_declared_dev_token_survives_dev_idp_collapse(self) -> None:
        """After setup_dev_idp collapses dev_token→jwt, posture still reports dev_token.

        ``setup_dev_idp`` stamps ``_declared_auth_mode='dev_token'`` and sets
        ``auth.mode='jwt'``.  The posture report must surface the declared
        opt-out, not the post-mutation jwt mode.
        """
        config = ServerConfig(auth=AuthConfig(mode="jwt"))
        # Simulate the post-dev_idp state.
        object.__setattr__(config.auth, "_declared_auth_mode", "dev_token")
        posture = compute_security_posture(config)
        assert posture["auth_mode"] == "dev_token"
        assert "auth.mode=dev_token" in posture["dev_opt_outs"]


# ---------------------------------------------------------------------------
# 3. _validate_environment_profile
# ---------------------------------------------------------------------------


class TestValidateEnvironmentProfile:
    def test_production_non_loopback_viewer_fallback_raises(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.auth.webhook_idp_on_failure = "viewer"
        with pytest.raises(ValueError, match="webhook_idp_on_failure"):
            _validate_environment_profile(config)

    def test_production_loopback_viewer_fallback_allowed(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(mode="dev_token", webhook_idp_on_failure="viewer"),
        )
        # Loopback default host → allowed (no raise).
        _validate_environment_profile(config)

    def test_dev_environment_viewer_fallback_allowed(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.environment = "dev"
        config.auth.webhook_idp_on_failure = "viewer"
        # environment=dev bypasses the production assertion.
        _validate_environment_profile(config)

    def test_production_non_loopback_deny_fallback_allowed(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.auth.webhook_idp_on_failure = "deny"
        # deny is the safe default → allowed even on a routable bind.
        _validate_environment_profile(config)

    def test_default_local_config_boots(self) -> None:
        # production + loopback + dev_token is the default local dev posture.
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        _validate_environment_profile(config)

    # -- 1f: unsigned-response IdP refusal on a routable production bind -----

    def test_production_non_loopback_webhook_unsigned_response_raises(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.auth.identity_provider = "webhook"
        object.__setattr__(config.auth, "webhook_idp_require_signed_response", False)
        with pytest.raises(ValueError, match="webhook_idp_require_signed_response"):
            _validate_environment_profile(config)

    def test_production_loopback_webhook_unsigned_response_allowed(self) -> None:
        config = ServerConfig(
            auth=AuthConfig(
                mode="dev_token",
                identity_provider="webhook",
                webhook_idp_require_signed_response=False,
            ),
        )
        # Loopback default host → allowed (no raise).
        _validate_environment_profile(config)

    def test_dev_environment_webhook_unsigned_response_allowed(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.environment = "dev"
        config.auth.identity_provider = "webhook"
        object.__setattr__(config.auth, "webhook_idp_require_signed_response", False)
        # environment=dev bypasses the production assertion.
        _validate_environment_profile(config)

    def test_production_non_loopback_webhook_signed_response_allowed(self) -> None:
        config, _ = _jwt_production_config(roles=["operator"])
        config.auth.identity_provider = "webhook"
        # require_signed_response True (default) → no raise on a routable bind.
        _validate_environment_profile(config)

    def test_production_non_loopback_local_idp_unsigned_field_allowed(self) -> None:
        # The unsigned-response refusal only applies to identity_provider=webhook.
        config, _ = _jwt_production_config(roles=["operator"])
        config.auth.identity_provider = "local"
        object.__setattr__(config.auth, "webhook_idp_require_signed_response", False)
        _validate_environment_profile(config)


# ---------------------------------------------------------------------------
# 4. startup posture log
# ---------------------------------------------------------------------------


class TestStartupPostureLog:
    def test_emits_one_posture_log_line(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="provide.uterm.server.app.factory")
        config = ServerConfig(auth=AuthConfig(mode="dev_token"))
        create_server_app(config, api_only=True)
        posture_lines = [r.getMessage() for r in caplog.records if "security_posture" in r.getMessage()]
        assert len(posture_lines) == 1
        assert "environment=production" in posture_lines[0]
        assert "auth_mode=dev_token" in posture_lines[0]
        assert "secure=" in posture_lines[0]


# ---------------------------------------------------------------------------
# 5. /api/security-posture endpoint (auth-gated)
# ---------------------------------------------------------------------------


class TestSecurityPostureCoarseFallback:
    """L10: when the authz service is unavailable on app.state, the endpoint
    fails closed to the coarse summary rather than leaking the full recon map.

    Exercises the production app (``create_server_app``) with the ``uterm_authz``
    service removed from app.state, so ``_posture_caller_is_privileged`` cannot
    resolve a privileged role and falls back to the coarse response.
    """

    def test_missing_authz_service_returns_coarse(self) -> None:
        config, token = _jwt_production_config(roles=["operator"])
        config.auth.webhook_idp_on_failure = "deny"
        app = create_server_app(config, api_only=True)
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            # Drop the authz service so the privilege check fails closed.
            app.state.uterm_authz = None
            r = client.get("/api/security-posture")
        assert r.status_code == 200
        body = r.json()
        # Coarse summary only — the full weakness map is withheld.
        assert "dev_opt_outs" not in body
        assert "warnings" not in body
        assert set(body) == {"environment", "secure"}


class TestSecurityPostureEndpoint:
    def test_anonymous_denied(self) -> None:
        # jwt mode → no token → anonymous → 401.
        config, _ = _jwt_production_config(roles=["operator"])
        # deny fallback so the production assertion permits the non-loopback bind.
        config.auth.webhook_idp_on_failure = "deny"
        app = create_server_app(config, api_only=True)
        with TestClient(app) as client:
            r = client.get("/api/security-posture")
        assert r.status_code in {401, 403}

    def test_authenticated_operator_gets_posture(self) -> None:
        config, token = _jwt_production_config(roles=["operator"])
        config.auth.webhook_idp_on_failure = "deny"
        app = create_server_app(config, api_only=True)
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            r = client.get("/api/security-posture")
        assert r.status_code == 200
        body = r.json()
        for field in (
            "environment",
            "bind_host",
            "is_loopback",
            "auth_mode",
            "dev_opt_outs",
            "idp_signing_required",
            "warnings",
            "secure",
        ):
            assert field in body
        assert body["auth_mode"] == "jwt"

    def test_admin_gets_full_posture(self) -> None:
        # Admin (most privileged) gets the full reconnaissance map.
        config, token = _jwt_production_config(roles=["admin"])
        config.auth.webhook_idp_on_failure = "deny"
        app = create_server_app(config, api_only=True)
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            r = client.get("/api/security-posture")
        assert r.status_code == 200
        body = r.json()
        assert "dev_opt_outs" in body
        assert "warnings" in body

    def test_viewer_gets_coarse_posture_without_recon_map(self) -> None:
        # L10: a lower-priv authenticated principal (viewer) must NOT see the
        # full dev_opt_outs / warnings reconnaissance map — only a coarse
        # environment + secure summary.
        config, token = _jwt_production_config(roles=["viewer"])
        config.auth.webhook_idp_on_failure = "deny"
        app = create_server_app(config, api_only=True)
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            r = client.get("/api/security-posture")
        assert r.status_code == 200
        body = r.json()
        # Coarse response: environment + secure only.
        assert body["environment"] == "production"
        assert "secure" in body
        # The full security weakness map must be withheld from a viewer.
        assert "dev_opt_outs" not in body
        assert "warnings" not in body
        assert "auth_mode" not in body
