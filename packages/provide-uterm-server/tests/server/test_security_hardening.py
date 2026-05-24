#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import os

import pytest

from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping


def test_dev_token_auth_mode_requires_loopback_host():
    """dev_token mode must refuse non-loopback hosts."""
    config = config_from_mapping({"server": {"host": "0.0.0.0"}, "auth": {"mode": "dev_token"}})
    with pytest.raises(RuntimeError, match="only permitted when server.host is a loopback"):
        create_server_app(config)


def test_dev_token_auth_mode_rejects_private_ip():
    """dev_token mode must refuse a non-loopback private IP."""
    config = config_from_mapping({"server": {"host": "192.168.1.100"}, "auth": {"mode": "dev_token"}})
    with pytest.raises(RuntimeError, match="only permitted when server.host is a loopback"):
        create_server_app(config)


def test_dev_token_auth_mode_permits_127_0_0_1():
    """dev_token mode permits 127.0.0.1."""
    config = config_from_mapping({"server": {"host": "127.0.0.1"}, "auth": {"mode": "dev_token"}})
    os.environ["UTERM_API_ONLY"] = "1"
    try:
        create_server_app(config)
    finally:
        del os.environ["UTERM_API_ONLY"]


def test_dev_token_auth_mode_permits_localhost():
    """dev_token mode permits localhost."""
    config = config_from_mapping({"server": {"host": "localhost"}, "auth": {"mode": "dev_token"}})
    os.environ["UTERM_API_ONLY"] = "1"
    try:
        create_server_app(config)
    finally:
        del os.environ["UTERM_API_ONLY"]


def test_dev_token_auth_mode_permits_ipv6_loopback():
    """dev_token mode permits ::1."""
    config = config_from_mapping({"server": {"host": "::1"}, "auth": {"mode": "dev_token"}})
    os.environ["UTERM_API_ONLY"] = "1"
    try:
        create_server_app(config)
    finally:
        del os.environ["UTERM_API_ONLY"]
