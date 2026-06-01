#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest
from pydantic import ValidationError

from provide.uterm.server.models import AuthConfig


def test_auth_config_idp_validation():
    # Valid local
    config = AuthConfig(identity_provider="local")
    assert config.identity_provider == "local"

    # Valid webhook. 1f: a webhook IdP now requires a signing secret (to verify
    # the signed response) unless response verification is explicitly disabled.
    config = AuthConfig(identity_provider="webhook", webhook_idp_require_signed_response=False)
    assert config.identity_provider == "webhook"

    # Invalid
    with pytest.raises(ValidationError):
        # We need to use a dict or similar because Literal validation happens at instantiation
        AuthConfig(identity_provider="invalid")


def test_auth_config_default_values():
    config = AuthConfig()
    # These will fail until Task 1 Step 3 is implemented
    assert hasattr(config, "identity_provider"), "AuthConfig missing identity_provider field"
    assert config.identity_provider == "local"
    assert config.delegate_roles is True
    assert config.webhook_idp_url is None
    assert config.webhook_idp_secret is None
    assert config.webhook_idp_timeout_s == 2.0


def test_auth_config_require_response_nonce_defaults_false():
    """L9: the enforce flag defaults False (backward-compat)."""
    assert AuthConfig().webhook_idp_require_response_nonce is False


def test_auth_config_require_response_nonce_accepts_true():
    """L9: the enforce flag can be enabled for HA / strict request-binding."""
    config = AuthConfig(webhook_idp_require_response_nonce=True)
    assert config.webhook_idp_require_response_nonce is True
