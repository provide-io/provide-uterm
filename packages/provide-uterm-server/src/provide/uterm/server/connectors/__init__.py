#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Connector exports for the hosted server app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from provide.uterm.server.connectors.base import SessionConnector
from provide.uterm.server.connectors.registry import (
    build_connector,
    register_connector,
    registered_types,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "KNOWN_CONNECTOR_TYPES",
    "SessionConnector",
    "build_connector",
    "register_connector",
    "registered_types",
]


def __getattr__(name: str) -> object:
    if name == "KNOWN_CONNECTOR_TYPES":
        return registered_types()
    if name == "TelnetSessionConnector":
        from provide.uterm.server.connectors.telnet import TelnetSessionConnector

        return TelnetSessionConnector
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
