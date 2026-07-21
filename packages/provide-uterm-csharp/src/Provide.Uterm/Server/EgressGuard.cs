//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

namespace Provide.Uterm.Server;

/// <summary>
/// Lightweight SSRF egress guard for graphical RFB dials (parity with Python/Go
/// connector egress: metadata always blocked; private/loopback/link-local when
/// <paramref name="blockPrivate"/> is true).
/// </summary>
public static class EgressGuard
{
    private static readonly IPAddress[] MetadataIps =
    {
        IPAddress.Parse("169.254.169.254"),
        IPAddress.Parse("100.100.100.200"),
    };

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
        if (IsMetadata(ip))
        {
            throw new InvalidOperationException(
                $"connector target '{host}' resolves to a blocked metadata address");
        }

        if (!blockPrivate)
        {
            return;
        }

        if (IPAddress.IsLoopback(ip)
            || ip.Equals(IPAddress.Any)
            || ip.Equals(IPAddress.IPv6Any)
            || IsPrivateOrLinkLocal(ip))
        {
            throw new InvalidOperationException(
                $"connector target '{host}' resolves to a blocked internal address");
        }
    }

    private static bool IsMetadata(IPAddress ip)
    {
        return MetadataIps.Any(m => m.Equals(ip));
    }

    private static bool IsPrivateOrLinkLocal(IPAddress ip)
    {
        if (ip.AddressFamily == AddressFamily.InterNetwork)
        {
            var b = ip.GetAddressBytes();
            // 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 100.64/10, 0/8, 224+/4
            if (b[0] == 10) return true;
            if (b[0] == 127) return true;
            if (b[0] == 0) return true;
            if (b[0] == 169 && b[1] == 254) return true;
            if (b[0] == 192 && b[1] == 168) return true;
            if (b[0] == 172 && b[1] >= 16 && b[1] <= 31) return true;
            if (b[0] == 100 && b[1] >= 64 && b[1] <= 127) return true;
            if (b[0] >= 224) return true;
            return false;
        }

        if (ip.AddressFamily == AddressFamily.InterNetworkV6)
        {
            if (ip.IsIPv6LinkLocal || ip.IsIPv6SiteLocal || ip.IsIPv6Multicast)
            {
                return true;
            }

            // Unique local fc00::/7
            var b = ip.GetAddressBytes();
            if ((b[0] & 0xfe) == 0xfc)
            {
                return true;
            }
        }

        return false;
    }
}
