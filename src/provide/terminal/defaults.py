#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Default host/port constants for provide-terminal transports."""

from __future__ import annotations


class TerminalDefaults:
    """Default host/port values used across provide-terminal transports and gateways.

    All constants are class-level and can be referenced as ``TerminalDefaults.X``
    without instantiation.  Override at the call site rather than modifying
    this class directly.
    """

    TELNET_HOST: str = "127.0.0.1"
    TELNET_PORT: int = 2102
    SSH_PORT: int = 2222
    GATEWAY_TELNET_PORT: int = 2112
    GATEWAY_SSH_PORT: int = 2222
