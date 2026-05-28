#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Transport adapters for provide-uterm."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.transports.base import ConnectionTransport
    from provide.uterm.transports.chaos import ChaosTransport
    from provide.uterm.transports.ssh import (
        SSHStreamReader,
        SSHStreamWriter,
        start_ssh_server,
    )
    from provide.uterm.transports.telnet import (
        TelnetClient,
        TelnetTransport,
        start_telnet_server,
    )
    from provide.uterm.transports.websocket import (
        WebSocketStreamReader,
        WebSocketStreamWriter,
    )

__all__ = [
    "ChaosTransport",
    "ConnectionTransport",
    "SSHStreamReader",
    "SSHStreamWriter",
    "TelnetClient",
    "TelnetTransport",
    "WebSocketStreamReader",
    "WebSocketStreamWriter",
    "start_ssh_server",
    "start_telnet_server",
]


def __getattr__(name: str) -> object:
    module_by_name = {
        "ConnectionTransport": "provide.uterm.transports.base",
        "ChaosTransport": "provide.uterm.transports.chaos",
        "SSHStreamReader": "provide.uterm.transports.ssh",
        "SSHStreamWriter": "provide.uterm.transports.ssh",
        "start_ssh_server": "provide.uterm.transports.ssh",
        "TelnetClient": "provide.uterm.transports.telnet",
        "TelnetTransport": "provide.uterm.transports.telnet",
        "start_telnet_server": "provide.uterm.transports.telnet",
        "WebSocketStreamReader": "provide.uterm.transports.websocket",
        "WebSocketStreamWriter": "provide.uterm.transports.websocket",
    }
    module_name = module_by_name.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = __import__(module_name, fromlist=[name])
    return getattr(module, name)
