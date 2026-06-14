#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Connector self-registration registry."""

from __future__ import annotations

import contextlib
import importlib
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from provide.uterm.server.connectors.base import SessionConnector


class SessionConnectorFactory(Protocol):
    def __call__(self, session_id: str, display_name: str, config: dict[str, Any], /) -> SessionConnector: ...


_registry: dict[str, SessionConnectorFactory] = {}
_BUILTIN_CLASSES: dict[str, tuple[str, str]] = {
    "shell": ("provide.uterm.server.connectors.shell", "ShellSessionConnector"),
    "ssh": ("provide.uterm.server.connectors.ssh", "SshSessionConnector"),
    "telnet": ("provide.uterm.server.connectors.telnet", "TelnetSessionConnector"),
    "websocket": ("provide.uterm.server.connectors.websocket", "WebSocketSessionConnector"),
}


def register_connector(name: str, cls: SessionConnectorFactory) -> None:
    """Register a connector class under a type name."""
    _registry[name] = cls


def build_connector(
    session_id: str,
    display_name: str,
    connector_type: str,
    config: dict[str, Any],
) -> SessionConnector:
    """Instantiate a connector by type name. Raises ValueError for unknown types."""
    cls = _registry.get(connector_type)
    if cls is None:
        builtin = _BUILTIN_CLASSES.get(connector_type)
        if builtin is not None:
            module_name, class_name = builtin
            with contextlib.suppress(ImportError):
                module = importlib.import_module(module_name)
                register_connector(connector_type, getattr(module, class_name))
            cls = _registry.get(connector_type)
    if cls is None:
        raise ValueError(f"unsupported connector_type: {connector_type!r}")
    return cls(session_id, display_name, config)


def registered_types() -> frozenset[str]:
    """Return the set of currently registered connector type names."""
    return frozenset(_registry)
