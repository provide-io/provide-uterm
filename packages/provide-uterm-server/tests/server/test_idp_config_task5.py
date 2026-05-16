import pytest
from pydantic import ValidationError

from provide.uterm.server.models import AuthConfig


def test_auth_config_idp_validation():
    # Valid local
    config = AuthConfig(identity_provider="local")
    assert config.identity_provider == "local"

    # Valid webhook
    config = AuthConfig(identity_provider="webhook")
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
