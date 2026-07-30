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

# RFC 6598 carrier-grade NAT space.  Named explicitly because CPython does NOT
# classify it as private:
#
#     >>> ipaddress.ip_address("100.64.0.1").is_private
#     False
#
# Every deny list derived from ``is_private`` / ``is_reserved`` / ``is_link_local``
# — which is how both guards below build theirs — therefore has a hole exactly
# here unless the range is added on top.  That hole is worth closing rather than
# inheriting: CGNAT is where carrier and container networks park real
# infrastructure, reachable from the server and unreachable from the internet,
# which is precisely the shape an SSRF pivot is looking for.
#
# Shared by the two derivation sites (``webhooks._address_allowed`` and
# ``egress._check_resolved_ip``) for the same reason ``_METADATA_IPS`` is: two
# hand-maintained copies of a security CIDR drift, and a drift here is silent.
# The two guards still apply it differently — see each call site — because they
# have different policies about internal destinations.
#
# Required across all four ports by ``conformance/EGRESS_GUARD.md`` §1.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


async def _resolve_host(hostname: str) -> tuple[str, ...]:
    """Resolve *hostname* to a tuple of IP address strings (async, via thread)."""
    infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    return tuple({str(info[4][0]) for info in infos})
