#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
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
    assert config.auth.mode == "dev_token"
    assert len(config.sessions) == 1
    assert config.sessions[0].session_id == "provide-shell"


def test_max_workers_default_and_valid_value():
    """Fix 2b: max_workers defaults to a generous global cap and accepts overrides."""
    config = UtermServerConfig()
    assert config.max_workers == 10000

    custom = UtermServerConfig(max_workers=5)
    assert custom.max_workers == 5


def test_max_workers_rejects_below_one():
    """Fix 2b: a max_workers < 1 is rejected by the validator."""
    with pytest.raises(ValueError, match="max_workers must be >= 1"):
        UtermServerConfig(max_workers=0)
