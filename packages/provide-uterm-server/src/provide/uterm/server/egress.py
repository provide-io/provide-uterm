#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Egress target validation for outbound connector connections."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from provide.uterm.server.webhooks import _METADATA_IPS, _resolve_host

if TYPE_CHECKING:
    from collections.abc import Mapping

# TTL for the per-host DNS cache used by assert_webhook_target_allowed.
_EGRESS_DNS_TTL_S: float = 60.0
# Bound the per-host DNS resolution so a slow / hostile resolver can't hang a
# session-create or webhook evaluation.  ``_resolve_host`` itself sets no
# timeout, so we wrap it in ``asyncio.wait_for`` at the guard call sites; the
# resulting ``TimeoutError`` is an ``OSError`` subclass and so flows through the
# fail-closed ``except OSError`` branch below.
_EGRESS_RESOLVE_TIMEOUT_S: float = 5.0
# host → (timestamp, resolved_addresses)
_resolve_cache: dict[str, tuple[float, tuple[str, ...]]] = {}

# NAT64 well-known prefix (RFC 6052).  Any address in this /96 carries an IPv4
# in its low 32 bits and, in a NAT64-enabled cluster, is actually translated to
# that IPv4 — so 64:ff9b::169.254.169.254 reaches the v4 metadata service.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

# Residual risk — M3 (DNS rebinding), partially mitigated:
#   The connector guard validates the IP a hostname resolves to at *create*
#   time, but the connector itself re-resolves the hostname at *connect* time.
#   A name with a TTL-0 record that flips to a metadata / internal IP between
#   create and connect could otherwise reach it.  Pinning the literal IP into
#   connector_config and connecting to the IP is the WRONG fix: it breaks TLS
#   SNI (wss://) and SSH host-key/known-hosts verification, silently disabling
#   MITM protection.  Instead the SSH and WebSocket connectors validate the
#   *actual* connected peer IP (``assert_ip_allowed``) right after the transport
#   handshake completes and BEFORE any application/PTY data flows — the handshake
#   still uses the original hostname, so SNI / host-key verification is intact.
#   That closes the rebinding window (we validate the IP we actually reached).
#   The telnet connector cannot reach the peer IP without a public accessor on
#   its (cross-package) transport, so it remains create-time-only for now.
#   The webhook path is bounded by the 60s ``_resolve_cache`` TTL above (a
#   rebind is caught on the next cache miss).


def _decode_embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address embedded in *ip*, for any embedding form, else None.

    Covers every IPv6 form that carries a reachable IPv4 so the metadata /
    private membership checks can be applied to the *decoded* address rather
    than the (membership-check-evading) IPv6 wrapper:

      * IPv4-mapped     ``::ffff:a.b.c.d``   -> ``ip.ipv4_mapped``
      * 6to4            ``2002:AABB:CCDD::``  -> ``ip.sixtofour``
      * NAT64 well-known ``64:ff9b::a.b.c.d`` -> low 32 bits (RFC 6052)
      * IPv4-compatible ``::a.b.c.d``         -> low 32 bits (deprecated)

    The deprecated IPv4-compatible form excludes ``::`` (unspecified) and
    ``::1`` (loopback), whose low bits are 0 / 1 and are handled by the normal
    IPv6 loopback / unspecified branches instead.
    """
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if (int(ip) >> 32) == 0 and (int(ip) & 0xFFFFFFFF) not in (0, 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


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
    addrs = tuple(await asyncio.wait_for(_resolve_host(host), _EGRESS_RESOLVE_TIMEOUT_S))
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
        # A resolution OSError (incl. socket.gaierror / timeout) must fail closed
        # rather than propagate out as an HTTP 500 / hang the request.
        try:
            addresses = await _resolve_cached(h)
        except OSError:
            raise EgressBlockedError(f"webhook target {url!r} could not be resolved") from None
        # An empty resolve must fail closed (parity with the connector guard);
        # otherwise the loop below never runs and the URL is silently allowed.
        if not addresses:
            raise EgressBlockedError(f"webhook target {url!r} could not be resolved") from None
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        # Decode any embedded-IPv4 IPv6 form (mapped / 6to4 / NAT64 / compat) to
        # its IPv4 so a wrapped metadata address can't slip past the membership
        # check.  In a NAT64 cluster 64:ff9b::169.254.169.254 is reachable.
        if isinstance(ip, ipaddress.IPv6Address):
            embedded = _decode_embedded_ipv4(ip)
            if embedded is not None:
                ip = embedded
        if ip in _METADATA_IPS:
            raise EgressBlockedError(f"webhook target {url!r} resolves to a blocked metadata address")


class EgressBlockedError(ValueError):
    """Raised when a connector target resolves to a forbidden address."""


def _check_resolved_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    block_private: bool,
    on_metadata: str,
    on_private: str,
) -> None:
    """Apply the metadata-always / private-when-flag policy to one resolved IP.

    Decodes any embedded-IPv4 IPv6 form (mapped / 6to4 / NAT64 / compat) to its
    IPv4 so a wrapped metadata/private address can't slip past the membership
    checks.  Cloud-metadata IPs are ALWAYS blocked (raising ``EgressBlockedError``
    with the *on_metadata* message); private / loopback / link-local / reserved /
    multicast / unspecified are blocked only when *block_private* is True (raising
    with the *on_private* message).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _decode_embedded_ipv4(ip)
        if embedded is not None:
            ip = embedded
    if ip in _METADATA_IPS:
        raise EgressBlockedError(on_metadata)
    if block_private and (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    ):
        raise EgressBlockedError(on_private)


def assert_ip_allowed(ip_str: str, *, block_private: bool) -> None:
    """Validate an ALREADY-RESOLVED literal peer IP (no DNS resolution).

    Used by connectors for post-connect peer-IP validation (M3 DNS-rebinding
    mitigation): after the transport handshake completes — but before any
    application/PTY data flows — the connector reads the real peer IP and runs
    it through this check, aborting if it is a blocked target.  Because the IP
    is already a literal there is no second DNS lookup (and so no rebinding
    window), and the handshake still used the original hostname, so TLS SNI /
    cert validation and SSH host-key/known-hosts verification stay intact.

    Cloud-metadata IPs are ALWAYS blocked; private/internal ranges are blocked
    only when *block_private* is True.
    """
    ip = ipaddress.ip_address(ip_str.strip().strip("[]"))
    _check_resolved_ip(
        ip,
        block_private=block_private,
        on_metadata=f"connector peer {ip_str!r} is a blocked metadata address",
        on_private=f"connector peer {ip_str!r} is a blocked internal address",
    )


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
        # A resolution OSError (incl. socket.gaierror / timeout) must fail closed
        # → EgressBlockedError (registry maps it to 422), not propagate as a 500.
        try:
            resolved = await asyncio.wait_for(_resolve_host(h), _EGRESS_RESOLVE_TIMEOUT_S)
        except OSError:
            raise EgressBlockedError(f"could not resolve connector host {host!r}") from None
        addresses = tuple(resolved)
        if not addresses:
            raise EgressBlockedError(f"could not resolve connector host {host!r}") from None
    for addr in addresses:
        # Decode any embedded-IPv4 IPv6 form (mapped / 6to4 / NAT64 / compat) and
        # apply the metadata (always) / private (when block_private) checks to
        # each resolved address — shared with the literal-IP ``assert_ip_allowed``.
        _check_resolved_ip(
            ipaddress.ip_address(addr),
            block_private=block_private,
            on_metadata=f"connector target {host!r} resolves to a blocked metadata address",
            on_private=f"connector target {host!r} resolves to a blocked internal address",
        )


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
