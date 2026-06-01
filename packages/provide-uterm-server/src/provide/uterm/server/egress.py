#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Egress target validation for outbound connector connections."""

from __future__ import annotations

import ipaddress
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from provide.uterm.server.webhooks import _METADATA_IPS, _resolve_host

if TYPE_CHECKING:
    from collections.abc import Mapping

# TTL for the per-host DNS cache used by assert_webhook_target_allowed.
_EGRESS_DNS_TTL_S: float = 60.0
# host → (timestamp, resolved_addresses)
_resolve_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


async def _resolve_cached(host: str) -> tuple[str, ...]:
    """Return resolved addresses for *host*, using a TTL-bounded in-process cache.

    Caching prevents a DNS storm when the same governance webhook URL is
    consulted on every incoming message.  The 60-second TTL is short enough
    that a rebinding attack that switches a name to a metadata IP will be
    caught on the next cache miss.
    """
    now = time.time()
    cached = _resolve_cache.get(host)
    if cached is not None and (now - cached[0]) < _EGRESS_DNS_TTL_S:
        return cached[1]
    addrs = tuple(await _resolve_host(host))
    _resolve_cache[host] = (now, addrs)
    return addrs


async def assert_webhook_target_allowed(url: str) -> None:
    """Raise EgressBlockedError if the webhook URL host resolves to a cloud-metadata IP.

    Metadata IPs are never a legitimate webhook target.  Private/internal hosts
    ARE allowed — a policy engine may be internal; HTTPS + TLS cert validation
    (enforced at config-load) cover the rest.  DNS results are cached for
    ``_EGRESS_DNS_TTL_S`` seconds to avoid per-request resolver storms.
    """
    host = urlparse(url).hostname
    if not host:
        return
    h = host.strip().strip("[]")
    try:
        addresses: tuple[str, ...] = (str(ipaddress.ip_address(h)),)
    except ValueError:
        addresses = await _resolve_cached(h)
        # An empty resolve must fail closed (parity with the connector guard);
        # otherwise the loop below never runs and the URL is silently allowed.
        if not addresses:
            raise EgressBlockedError(f"webhook target {url!r} could not be resolved") from None
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        # Normalize IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) to its IPv4
        # form so a mapped metadata address can't slip past the membership check.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip in _METADATA_IPS:
            raise EgressBlockedError(f"webhook target {url!r} resolves to a blocked metadata address")


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
        # Normalize IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) to its IPv4
        # form so a mapped metadata/private address can't slip past the checks.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
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


async def assert_session_egress_allowed(
    connector_type: str,
    connector_config: Mapping[str, Any],
    *,
    block_private: bool,
) -> None:
    """Egress-guard a session's connector target, given its type and config.

    This is the single source of truth for deriving the outbound host from a
    connector type + config and applying ``assert_connector_target_allowed``.
    Called from the SessionRegistry chokepoint (so every create/update path is
    covered) and from the /api/connect route (defense-in-depth + synchronous
    422 mapping).

    Host derivation:
      * ssh / telnet  -> ``connector_config["host"]``
      * websocket     -> ``urlparse(connector_config["url"]).hostname`` (when a
                         ``url`` is present)

    Connector types that take no user-supplied host or URL (shell / local /
    pty / the internal tunnel websocket with no url) yield no host and are
    intentionally left unguarded — there is nothing attacker-controlled to
    validate.  When a host IS derived it is passed to
    ``assert_connector_target_allowed`` which raises ``EgressBlockedError``.
    """
    target_host: str | None = None
    if connector_type in {"ssh", "telnet"}:
        host = connector_config.get("host")
        target_host = str(host) if host is not None else None
    elif connector_type == "websocket":
        url = connector_config.get("url")
        if url is not None:
            target_host = urlparse(str(url)).hostname
    if target_host is not None:
        await assert_connector_target_allowed(target_host, block_private=block_private)
