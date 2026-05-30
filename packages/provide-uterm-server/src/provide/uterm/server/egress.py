#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Egress target validation for outbound connector connections."""

from __future__ import annotations

import ipaddress

from provide.uterm.server.webhooks import _METADATA_IPS, _resolve_host


class EgressBlockedError(ValueError):
    """Raised when a connector target resolves to a forbidden address."""


async def assert_connector_target_allowed(host: str, *, block_private: bool) -> None:
    """Validate a connector target host (literal IP or DNS name).

    Cloud-metadata IPs are ALWAYS blocked (never a legitimate terminal target).
    Private / loopback / link-local / reserved / multicast / unspecified are
    blocked only when *block_private* is True (multi-tenant / hosted posture);
    by default connectors may reach internal hosts, which is their purpose.
    Every address a DNS name resolves to is checked (defeats rebinding to a
    metadata IP at minimum).
    """
    h = host.strip().strip("[]")
    try:
        addresses: tuple[str, ...] = (str(ipaddress.ip_address(h)),)
    except ValueError:
        resolved = await _resolve_host(h)
        addresses = tuple(resolved)
        if not addresses:
            raise EgressBlockedError(f"could not resolve connector host {host!r}") from None
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if ip in _METADATA_IPS:
            raise EgressBlockedError(f"connector target {host!r} resolves to a blocked metadata address")
        if block_private and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            raise EgressBlockedError(f"connector target {host!r} resolves to a blocked internal address")
