#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Default host/port constants for provide-terminal transports."""

from __future__ import annotations


class TerminalDefaults:
    TELNET_HOST: str = "0.0.0.0"  # nosec B104
    TELNET_PORT: int = 2102
    SSH_PORT: int = 2222
