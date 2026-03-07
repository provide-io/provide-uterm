#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Connector exports for the hosted server app."""

from __future__ import annotations

from typing import Any

from provide.terminal.server.connectors.base import SessionConnector
from provide.terminal.server.connectors.demo import DemoSessionConnector
from provide.terminal.server.connectors.telnet import TelnetSessionConnector

__all__ = [
    "DemoSessionConnector",
    "SessionConnector",
    "TelnetSessionConnector",
    "build_connector",
]


def build_connector(
    session_id: str, display_name: str, connector_type: str, config: dict[str, Any]
) -> SessionConnector:
    """Instantiate a built-in connector by type."""
    if connector_type == "demo":
        return DemoSessionConnector(session_id, display_name)
    if connector_type == "telnet":
        return TelnetSessionConnector(session_id, display_name, config)
    if connector_type == "ssh":
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        return SshSessionConnector(session_id, display_name, config)
    raise RuntimeError(f"unsupported connector_type: {connector_type}")
