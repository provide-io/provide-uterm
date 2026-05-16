import pytest

from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.config_schema import UtermServerConfig


def test_config_strict_validation():
    # Valid config should pass
    valid_data = {"server": {"port": 9000}, "auth": {"mode": "jwt"}}
    config = config_from_mapping(valid_data)
    assert config.server.port == 9000
    assert config.auth.mode == "jwt"

    # Unknown top-level field should fail
    invalid_data = {"unknown_field": "value"}
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        config_from_mapping(invalid_data)

    # Unknown field in a section should fail
    invalid_section_data = {"server": {"port": 9000, "unknown_server_field": "value"}}
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        config_from_mapping(invalid_section_data)


def test_default_config():
    config = UtermServerConfig()
    # Check default overrides from Task 1
    assert config.auth.mode == "dev"
    assert len(config.sessions) == 1
    assert config.sessions[0].session_id == "provide-shell"
