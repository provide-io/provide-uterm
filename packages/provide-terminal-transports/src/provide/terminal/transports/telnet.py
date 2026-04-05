#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Telnet transport for provide-terminal.

Provides:
- :class:`TelnetClient` — thin client wrapper around ``asyncio.open_connection``
  with IAC constants and negotiation helpers.
- :class:`TelnetTransport` — full RFC 854 client implementing
  :class:`~provide.terminal.transports.base.ConnectionTransport`.
- Telnet protocol constants: ``IAC``, ``WILL``, ``WONT``, ``DO``, ``DONT``, ``SB``, ``SE``.
- :func:`start_telnet_server` — asyncio TCP server (defined in
  :mod:`~provide.terminal.transports.telnet_server`, re-exported here).
"""

from provide.terminal.transports.telnet_client import TelnetClient as TelnetClient
from provide.terminal.transports.telnet_server import start_telnet_server as start_telnet_server
from provide.terminal.transports.telnet_transport import TelnetTransport as TelnetTransport

# ---------------------------------------------------------------------------
# Telnet protocol constants
# ---------------------------------------------------------------------------

IAC: int = 255  # Interpret As Command
WILL: int = 251  # Will perform option
WONT: int = 252  # Won't perform option
DO: int = 253  # Do perform option
DONT: int = 254  # Don't perform option
SB: int = 250  # Sub-negotiation Begin
SE: int = 240  # Sub-negotiation End

# Telnet options
ECHO: int = 1  # Echo
SGA: int = 3  # Suppress Go Ahead
NAWS: int = 31  # Negotiate About Window Size
LINEMODE: int = 34  # Linemode

# Terminal type subnegotiation
OPT_TTYPE: int = 24
TTYPE_IS: int = 0

# Telnet option codes (aliases for TelnetTransport use)
OPT_BINARY: int = 0
OPT_ECHO: int = ECHO
OPT_SGA_OPT: int = SGA
OPT_NAWS: int = NAWS

__all__ = ["TelnetClient", "TelnetTransport", "start_telnet_server"]
