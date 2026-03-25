#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Connector exports for the hosted server app."""

from __future__ import annotations

import contextlib

from provide.terminal.server.connectors.registry import build_connector, register_connector, registered_types

from provide.terminal.server.connectors.base import SessionConnector
from provide.terminal.server.connectors.telnet import TelnetSessionConnector  # registers "telnet"

__all__ = [
    "KNOWN_CONNECTOR_TYPES",
    "SessionConnector",
    "ShellSessionConnector",
    "SshSessionConnector",
    "TelnetSessionConnector",
    "UshellConnector",
    "WebSocketSessionConnector",
    "build_connector",
]

with contextlib.suppress(ImportError):
    from provide.terminal.server.connectors.shell import ShellSessionConnector  # registers "shell"
with contextlib.suppress(ImportError):
    from provide.terminal.server.connectors.ssh import SshSessionConnector  # registers "ssh"
with contextlib.suppress(ImportError):
    from provide.terminal.server.connectors.websocket import WebSocketSessionConnector  # registers "websocket"
with contextlib.suppress(ImportError):
    # register_connector is always available (from our own registry.py);
    # only the UshellConnector import is optional — it requires provide-shell installed.
    from provide.shell.terminal._connector import UshellConnector

    register_connector("ushell", UshellConnector)

# Derived from the registry — reflects whatever connectors are available in this env.
KNOWN_CONNECTOR_TYPES: frozenset[str] = registered_types()
