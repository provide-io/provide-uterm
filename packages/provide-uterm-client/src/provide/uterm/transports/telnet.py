#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Telnet transport for provide-uterm.

Provides:
- :class:`TelnetClient` — thin client wrapper around ``asyncio.open_connection``
  with IAC constants and negotiation helpers.
- :class:`TelnetTransport` — full RFC 854 client implementing
  :class:`~provide.uterm.transports.base.ConnectionTransport`.
- Telnet protocol constants: ``IAC``, ``WILL``, ``WONT``, ``DO``, ``DONT``, ``SB``, ``SE``.
- :func:`start_telnet_server` — asyncio TCP server (defined in
  :mod:`~provide.uterm.transports.telnet_server`, re-exported here).
"""

# Telnet protocol constants — re-exported from the dependency-free leaf module.
from provide.uterm.transports._telnet_const import (
    DO as DO,
)
from provide.uterm.transports._telnet_const import (
    DONT as DONT,
)
from provide.uterm.transports._telnet_const import (
    ECHO as ECHO,
)
from provide.uterm.transports._telnet_const import (
    IAC as IAC,
)
from provide.uterm.transports._telnet_const import (
    LINEMODE as LINEMODE,
)
from provide.uterm.transports._telnet_const import (
    NAWS as NAWS,
)
from provide.uterm.transports._telnet_const import (
    OPT_BINARY as OPT_BINARY,
)
from provide.uterm.transports._telnet_const import (
    OPT_ECHO as OPT_ECHO,
)
from provide.uterm.transports._telnet_const import (
    OPT_NAWS as OPT_NAWS,
)
from provide.uterm.transports._telnet_const import (
    OPT_SGA_OPT as OPT_SGA_OPT,
)
from provide.uterm.transports._telnet_const import (
    OPT_TTYPE as OPT_TTYPE,
)
from provide.uterm.transports._telnet_const import (
    SB as SB,
)
from provide.uterm.transports._telnet_const import (
    SE as SE,
)
from provide.uterm.transports._telnet_const import (
    SGA as SGA,
)
from provide.uterm.transports._telnet_const import (
    TTYPE_IS as TTYPE_IS,
)
from provide.uterm.transports._telnet_const import (
    WILL as WILL,
)
from provide.uterm.transports._telnet_const import (
    WONT as WONT,
)
from provide.uterm.transports.telnet_client import TelnetClient as TelnetClient
from provide.uterm.transports.telnet_server import (
    start_telnet_server as start_telnet_server,
)
from provide.uterm.transports.telnet_transport import (
    TelnetTransport as TelnetTransport,
)

__all__ = ["TelnetClient", "TelnetTransport", "start_telnet_server"]
