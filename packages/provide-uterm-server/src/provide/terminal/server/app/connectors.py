#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Built-in and external connector registration for the hosted terminal server."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.terminal.server.models import ServerConfig

logger = get_logger(__name__)


def _register_builtin_connectors(config: ServerConfig) -> None:
    """Register standard terminal connectors and any external plugins."""
    from provide.terminal.server.connectors import register_connector

    # 1. Built-in AGPL Connectors
    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.shell import ShellSessionConnector

        register_connector("shell", ShellSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.ssh import SshSessionConnector

        register_connector("ssh", SshSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.telnet import TelnetSessionConnector

        register_connector("telnet", TelnetSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.server.connectors.websocket import WebSocketSessionConnector

        register_connector("websocket", WebSocketSessionConnector)

    with contextlib.suppress(ImportError):
        from provide.terminal.shell.terminal import UshellConnector

        register_connector("ushell", UshellConnector)

    with contextlib.suppress(ImportError):
        import provide.terminal.pty.connector

    with contextlib.suppress(ImportError):
        import provide.terminal.pty.capture_connector  # noqa: F401

    # 2. External Plugin Connectors
    import importlib

    for module_path in config.governance.external_connectors:
        try:
            importlib.import_module(module_path)
            logger.info("connector_plugin_loaded module=%s", module_path)
        except ImportError:
            logger.error("connector_plugin_load_failed module=%s", module_path)
