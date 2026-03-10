#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Connector exports for the hosted server app."""

from __future__ import annotations

import contextlib
from typing import Any

from provide.terminal.server.connectors.base import SessionConnector
from provide.terminal.server.connectors.telnet import TelnetSessionConnector

__all__ = [
    "KNOWN_CONNECTOR_TYPES",
    "SessionConnector",
    "ShellSessionConnector",
    "SshSessionConnector",
    "TelnetSessionConnector",
    "build_connector",
]

# ShellSessionConnector and SshSessionConnector are conditionally imported
# at module level for __all__ discoverability; callers that need them at
# runtime should catch ImportError if their deps are absent.
with contextlib.suppress(ImportError):
    from provide.terminal.server.connectors.shell import ShellSessionConnector
with contextlib.suppress(ImportError):
    from provide.terminal.server.connectors.ssh import SshSessionConnector

# Connector types recognised by build_connector().  Used by the registry to
# validate connector_type at session-creation time so callers get a 422 instead
# of discovering the error asynchronously via lifecycle_state == "error".
KNOWN_CONNECTOR_TYPES: frozenset[str] = frozenset({"shell", "telnet", "ssh"})


def build_connector(
    session_id: str, display_name: str, connector_type: str, config: dict[str, Any]
) -> SessionConnector:
    """Instantiate a built-in connector by type."""
    if connector_type == "shell":
        from provide.terminal.server.connectors.shell import ShellSessionConnector

        return ShellSessionConnector(session_id, display_name, config)
    if connector_type == "telnet":
        return TelnetSessionConnector(session_id, display_name, config)
    if connector_type == "ssh":
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        return SshSessionConnector(session_id, display_name, config)
    raise ValueError(f"unsupported connector_type: {connector_type}")
