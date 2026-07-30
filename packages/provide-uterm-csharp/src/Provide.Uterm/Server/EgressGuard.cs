//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System;
using System.Net;
using System.Threading;
using System.Threading.Tasks;

namespace Provide.Uterm.Server;

/// <summary>
/// SSRF egress guard for connector / graphical RFB dials. Parity with the
/// reference's <c>assert_connector_target_allowed</c>: cloud metadata is always
/// refused; private, loopback, link-local, multicast, unspecified and
/// IANA-reserved space is refused only when <c>blockPrivate</c> is set.
/// </summary>
/// <remarks>
/// <para>
/// Classification is delegated to <see cref="EgressAddressPolicy"/> — the same
/// classifier the webhook guard uses. It used to carry its own hand-rolled range
/// list, and two classifiers answering the same question is a drift generator:
/// the copy here was the permissive one, and it was permissive in ways that
/// mattered. It held only the two IPv4 metadata addresses, so
/// <c>fd00:ec2::254</c> (EC2 IPv6 metadata) was reachable; it did not decode the
/// IPv4-carrying IPv6 forms, so <c>64:ff9b::169.254.169.254</c> — which reaches
/// the v4 metadata service on a NAT64 cluster — read as "some IPv6 address"; and
/// its IPv6 arm knew nothing of the reserved blocks or the documentation ranges
/// that CPython's <c>is_private</c> / <c>is_reserved</c> cover.
/// </para>
/// <para>
/// The <c>blockPrivate</c> distinction is preserved exactly, because it is the
/// whole point of the flag: the reference's <c>_check_resolved_ip</c>
/// (<c>.../server/egress.py</c>) applies that entire set <em>only</em> when
/// <c>block_private</c> is true, and refuses metadata before consulting it. With
/// the flag off, connectors may reach internal hosts — which is what connectors
/// are for — so the guard must stay permissive there rather than inheriting the
/// webhook path's posture.
/// </para>
/// </remarks>
public static class EgressGuard
{
    public static async Task AssertConnectorTargetAllowedAsync(
        string host,
        bool blockPrivate = true,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(host))
        {
            throw new InvalidOperationException("empty connector host");
        }

        host = host.Trim().Trim('[', ']');
        if (IPAddress.TryParse(host, out var literal))
        {
            CheckIp(literal, host, blockPrivate);
            return;
        }

        IPAddress[] addrs;
        try
        {
            addrs = await Dns.GetHostAddressesAsync(host, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"could not resolve connector host '{host}'", ex);
        }

        if (addrs.Length == 0)
        {
            throw new InvalidOperationException($"could not resolve connector host '{host}'");
        }

        foreach (var ip in addrs)
        {
            CheckIp(ip, host, blockPrivate);
        }
    }

    private static void CheckIp(IPAddress ip, string host, bool blockPrivate)
    {
        // Decode first, classify second: an IPv6 wrapper carrying a metadata or
        // private IPv4 has to be judged on what it actually reaches, not on the
        // family of the literal it was written as.
        var target = EgressAddressPolicy.DecodeEmbeddedIPv4(ip) ?? ip;

        if (EgressAddressPolicy.IsMetadata(target))
        {
            throw new InvalidOperationException(
                $"connector target '{host}' resolves to a blocked metadata address");
        }

        if (!blockPrivate)
        {
            return;
        }

        // Loopback is asked separately because EgressAddressPolicy keeps it
        // separate — the webhook guard has an operator-facing key that turns it
        // alone back on (EGRESS_GUARD.md §2). The connector guard has no such
        // key: under blockPrivate the whole set goes, loopback with it, matching
        // the reference's single `ip.is_private or ip.is_loopback or …` test.
        if (EgressAddressPolicy.IsLoopback(target)
            || EgressAddressPolicy.IsBlockedPrivate(target))
        {
            throw new InvalidOperationException(
                $"connector target '{host}' resolves to a blocked internal address");
        }
    }

    // RFC 6598 CGNAT used to be refused here and here only, because at the time
    // this guard was consolidated no other port blocked it and adding it to the
    // shared list would have made *this* port's webhook guard the odd one out.
    // Every port blocks it now (EGRESS_GUARD.md §1 pins it), so it moved into
    // EgressAddressPolicy.BlockedPrivateV4 and this guard inherits it — which is
    // what makes the webhook path refuse it too. Keeping a private copy here
    // would have left the two paths disagreeing again, one range at a time.
}
