#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lightweight network utilities shared by egress validation and webhook checks.

Kept in a module with only stdlib + asyncio deps so that egress.py — which is
imported by TelnetSessionConnector at connection time — does NOT pull in the
hub/bridge stack (and transitively fastapi) just to validate an outbound IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

# Cloud-metadata service IPs that outbound connections should never reach.
# Used by assert_ip_allowed (egress.py) and webhook URL validation (webhooks.py).
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


async def _resolve_host(hostname: str) -> tuple[str, ...]:
    """Resolve *hostname* to a tuple of IP address strings (async, via thread)."""
    infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    return tuple({str(info[4][0]) for info in infos})
