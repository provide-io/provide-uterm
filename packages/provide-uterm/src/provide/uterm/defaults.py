#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Default host/port constants for provide-uterm transports."""

from __future__ import annotations

from pathlib import Path


class TerminalDefaults:
    """Default host/port values used across provide-uterm transports and gateways.

    All constants are class-level and can be referenced as ``TerminalDefaults.X``
    without instantiation.  Override at the call site rather than modifying
    this class directly.
    """

    TELNET_HOST: str = "127.0.0.1"
    TELNET_PORT: int = 2102
    SSH_PORT: int = 2222
    GATEWAY_TELNET_PORT: int = 2112
    GATEWAY_SSH_PORT: int = 2222

    BIND_ALL: str = "0.0.0.0"  # nosec B104  # bind-all for gateway/proxy
    PROXY_PORT: int = 8765  # uterm proxy default HTTP listen port
    PROXY_WS_PATH: str = "/ws/terminal"  # uterm proxy default WebSocket path
    SERVER_HOST: str = "127.0.0.1"  # provide-uterm-server default bind host
    SERVER_PORT: int = 8780  # provide-uterm-server default port
    TELNET_REMOTE_PORT: int = 23  # default remote telnet port (connect-to)
    SSH_REMOTE_PORT: int = 22  # default remote SSH port (connect-to)
    WS_PING_INTERVAL: int = 20  # websocket ping interval (seconds)
    WS_PING_TIMEOUT: int = 20  # websocket ping response timeout (seconds)
    WS_CLOSE_TIMEOUT: int = 10  # websocket close timeout (seconds)
    RECONNECT_MAX_RETRIES: int = 5  # reconnect attempts before giving up
    RECONNECT_BASE_BACKOFF_S: float = 0.5  # initial reconnect backoff (seconds)
    RECONNECT_MAX_BACKOFF_S: float = 30.0  # ceiling for reconnect backoff (seconds)

    @classmethod
    def token_file(cls) -> Path:
        """Default resume-token file path (~/.uterm/session_token)."""
        return Path.home() / ".uterm" / "session_token"
