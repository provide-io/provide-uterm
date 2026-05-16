#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest

from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping


def test_dev_auth_mode_requires_loopback_host():
    """Verify that mode='dev' requires host to be a loopback address."""
    config = config_from_mapping({"server": {"host": "0.0.0.0"}, "auth": {"mode": "dev"}})

    with pytest.raises(RuntimeError, match="auth.mode='dev' is only permitted when server.host is a loopback address"):
        create_server_app(config)


def test_none_auth_mode_requires_loopback_host():
    """Verify that mode='none' requires host to be a loopback address."""
    config = config_from_mapping({"server": {"host": "192.168.1.100"}, "auth": {"mode": "none"}})

    with pytest.raises(RuntimeError, match="auth.mode='none' is only permitted when server.host is a loopback address"):
        create_server_app(config)


def test_dev_auth_mode_permits_127_0_0_1():
    """Verify that mode='dev' permits host='127.0.0.1'."""
    config = config_from_mapping({"server": {"host": "127.0.0.1"}, "auth": {"mode": "dev"}})
    # Should not raise, but might fail later if frontend assets are missing
    # so we use UTERM_API_ONLY=1 to skip asset check.
    import os

    os.environ["UTERM_API_ONLY"] = "1"
    try:
        create_server_app(config)
    finally:
        del os.environ["UTERM_API_ONLY"]


def test_dev_auth_mode_permits_localhost():
    """Verify that mode='dev' permits host='localhost'."""
    config = config_from_mapping({"server": {"host": "localhost"}, "auth": {"mode": "dev"}})
    import os

    os.environ["UTERM_API_ONLY"] = "1"
    try:
        create_server_app(config)
    finally:
        del os.environ["UTERM_API_ONLY"]


def test_dev_auth_mode_permits_ipv6_loopback():
    """Verify that mode='dev' permits host='::1'."""
    config = config_from_mapping({"server": {"host": "::1"}, "auth": {"mode": "dev"}})
    import os

    os.environ["UTERM_API_ONLY"] = "1"
    try:
        create_server_app(config)
    finally:
        del os.environ["UTERM_API_ONLY"]
