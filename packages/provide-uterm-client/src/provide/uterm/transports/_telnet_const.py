#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Telnet IAC / option byte constants (RFC 854 and friends).

Dependency-free leaf module: it has zero intra-package imports, so importing
it from any transport module cannot introduce a circular import. Each constant
is defined here exactly once; the transport modules import the names they use.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Telnet command bytes
# ---------------------------------------------------------------------------

IAC: int = 255  # Interpret As Command
WILL: int = 251  # Will perform option
WONT: int = 252  # Won't perform option
DO: int = 253  # Do perform option
DONT: int = 254  # Don't perform option
SB: int = 250  # Sub-negotiation Begin
SE: int = 240  # Sub-negotiation End

# ---------------------------------------------------------------------------
# Telnet options
# ---------------------------------------------------------------------------

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
