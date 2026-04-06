#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Transport adapters for provide-terminal."""

from __future__ import annotations

from provide.terminal.transports.base import ConnectionTransport
from provide.terminal.transports.chaos import ChaosTransport
from provide.terminal.transports.ssh import (
    SSHStreamReader,
    SSHStreamWriter,
    start_ssh_server,
)
from provide.terminal.transports.telnet import (
    TelnetClient,
    TelnetTransport,
    start_telnet_server,
)
from provide.terminal.transports.websocket import (
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
