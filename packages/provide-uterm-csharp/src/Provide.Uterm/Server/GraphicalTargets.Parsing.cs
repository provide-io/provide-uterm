// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

using System.Globalization;
using System.Net;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Server;

public static class GraphicalTargetParsing
{
    public static (string Host, int Port) ParseRfbEndpoint(string? rawEndpoint)
    {
        if (string.IsNullOrWhiteSpace(rawEndpoint))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "endpoint is required for protocol rfb");
        }

        var endpoint = NormalizeRfbEndpoint(rawEndpoint);
        if (!endpoint.StartsWith("rfb://", StringComparison.OrdinalIgnoreCase))
        {
            if (!endpoint.Contains(':'))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Invalid,
                    "invalid endpoint; expected host:port or rfb://host:port");
            }

            endpoint = "rfb://" + endpoint;
        }

        var (host, rawPort) = HostAndPortOf(NetlocOf(endpoint));
        if (host is null)
        {
            throw new GraphicalTargetException(
                GraphicalTargetErrorCode.Invalid,
                "invalid endpoint; expected host:port or rfb://host:port");
        }

        return (host, PortOf(rawPort));
    }

    /// <summary>
    /// Validate a litevirt gRPC endpoint. Unlike rfb this carries no scheme —
    /// it is a plain host:port target (optionally prefixed with dns:///). We
    /// require it non-empty and shaped like host:port with a valid port.
    /// </summary>
    public static (string Host, int Port) ParseLitevirtEndpoint(string? rawEndpoint)
    {
        if (string.IsNullOrWhiteSpace(rawEndpoint))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "endpoint is required for protocol litevirt");
        }

        var endpoint = NormalizeEndpoint(rawEndpoint);
        var (host, rawPort) = HostAndPortOf(NetlocOf("grpc://" + endpoint));
        if (host is null)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint; expected host:port");
        }

        return (host, PortOf(rawPort));
    }

    private static string NormalizeEndpoint(string? rawEndpoint)
    {
        var endpoint = rawEndpoint!.Trim();
        if (endpoint.StartsWith("dns:///", StringComparison.OrdinalIgnoreCase))
        {
            return endpoint["dns:///".Length..];
        }

        return endpoint;
    }

    private static string NormalizeRfbEndpoint(string? rawEndpoint) =>
        NormalizeEndpoint(rawEndpoint).Trim();

    private static readonly char[] NetlocStopChars = ['/', '?', '#'];
    private static readonly Regex HextetPattern = new(@"^[0-9A-Fa-f]{1,4}$", RegexOptions.Compiled);
    private static readonly Regex OctetPattern = new(@"^[0-9]{1,3}$", RegexOptions.Compiled);
    private static readonly Regex IpvFuturePattern = new(@"^v[0-9A-Fa-f]+\..+$", RegexOptions.Compiled);
    private const int HextetCount = 8;
    private const int MaxAddressChars = 45;

    private static string NetlocOf(string url)
    {
        var cleaned = url.Replace("\t", "", StringComparison.Ordinal).Replace("\r", "", StringComparison.Ordinal).Replace(
            "\n",
            "",
            StringComparison.Ordinal);
        var scheme = cleaned.IndexOf("://", StringComparison.Ordinal);
        var after = scheme == -1 ? cleaned : cleaned[(scheme + 3)..];
        var stop = after.IndexOfAny(NetlocStopChars);
        return stop == -1 ? after : after[..stop];
    }

    private static (string? Host, string? Port) HostAndPortOf(string netloc)
    {
        if (!BracketsAreValid(netloc))
        {
            return (null, null);
        }

        var at = netloc.LastIndexOf("@", StringComparison.Ordinal);
        var hostInfo = at == -1 ? netloc : netloc[(at + 1)..];
        string rawHost;
        string rawPort;

        var open = hostInfo.IndexOf("[", StringComparison.Ordinal);
        if (open == -1)
        {
            var colon = hostInfo.IndexOf(":", StringComparison.Ordinal);
            rawHost = colon == -1 ? hostInfo : hostInfo[..colon];
            rawPort = colon == -1 ? "" : hostInfo[(colon + 1)..];
        }
        else
        {
            var bracketed = hostInfo[(open + 1)..];
            var close = bracketed.IndexOf("]", StringComparison.Ordinal);
            rawHost = close == -1 ? bracketed : bracketed[..close];
            var afterHost = close == -1 ? "" : bracketed[(close + 1)..];
            var colon = afterHost.IndexOf(":", StringComparison.Ordinal);
            rawPort = colon == -1 ? "" : afterHost[(colon + 1)..];
        }

        if (rawHost.Length == 0)
        {
            return (null, null);
        }

        var zone = rawHost.IndexOf("%", StringComparison.Ordinal);
        var host = zone == -1
            ? rawHost.ToLowerInvariant()
            : rawHost[..zone].ToLowerInvariant() + rawHost[zone..];
        return (host, rawPort.Length == 0 ? null : rawPort);
    }

    private static int PortOf(string? rawPort)
    {
        if (rawPort is null || !rawPort.All(char.IsDigit))
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint port");
        }

        if (!int.TryParse(rawPort, NumberStyles.Integer, CultureInfo.InvariantCulture, out var port) || port is < 1 or > 65535)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, "invalid endpoint port");
        }

        return port;
    }

    private static bool BracketsAreValid(string netloc)
    {
        var opened = netloc.Contains("[", StringComparison.Ordinal);
        var closed = netloc.Contains("]", StringComparison.Ordinal);
        if (opened != closed)
        {
            return false;
        }

        if (!opened)
        {
            return true;
        }

        var at = netloc.LastIndexOf("@", StringComparison.Ordinal);
        var hostInfo = at == -1 ? netloc : netloc[(at + 1)..];
        var open = hostInfo.IndexOf("[", StringComparison.Ordinal);
        if (open == -1)
        {
            var colon = hostInfo.IndexOf(":", StringComparison.Ordinal);
            var host = colon == -1 ? hostInfo : hostInfo[..colon];
            return IsBracketedHost(host);
        }

        if (open != 0)
        {
            return false;
        }

        var bracketed = hostInfo[(open + 1)..];
        var close = bracketed.IndexOf("]", StringComparison.Ordinal);
        if (close == -1)
        {
            return IsBracketedHost(bracketed);
        }

        var after = bracketed[(close + 1)..];
        return (after == "" || after.StartsWith(":", StringComparison.Ordinal)) && IsBracketedHost(bracketed[..close]);
    }

    private static bool IsIpv4Address(string text)
    {
        var octets = text.Split('.');
        if (octets.Length != 4)
        {
            return false;
        }

        return octets.All(octet =>
            OctetPattern.IsMatch(octet) &&
            (octet == "0" || !octet.StartsWith("0", StringComparison.Ordinal)) &&
            int.TryParse(octet, NumberStyles.Integer, CultureInfo.InvariantCulture, out var value) &&
            value <= 255);
    }

    private static bool IsIpv6Address(string text)
    {
        var zoneAt = text.IndexOf("%", StringComparison.Ordinal);
        if (zoneAt == text.Length - 1)
        {
            return false;
        }

        var zone = zoneAt == -1 ? "" : text[(zoneAt + 1)..];
        if (zone != "" && zone.Contains("%", StringComparison.Ordinal))
        {
            return false;
        }

        var address = zoneAt == -1 ? text : text[..zoneAt];
        if (address.Length == 0 || address.Length > MaxAddressChars)
        {
            return false;
        }

        var all = address.Split(':');
        var parts = new List<string>(all.Length);
        if (all.Length > HextetCount + 2)
        {
            foreach (var item in all.Take(HextetCount + 1))
            {
                parts.Add(item);
            }

            parts.Add(string.Join(':', all.Skip(HextetCount + 1)));
        }
        else
        {
            parts.AddRange(all);
        }

        if (parts.Count < 3)
        {
            return false;
        }

        if (parts[^1].Contains(".", StringComparison.Ordinal))
        {
            if (!IsIpv4Address(parts[^1]))
            {
                return false;
            }

            parts.RemoveAt(parts.Count - 1);
            parts.Add("0");
            parts.Add("0");
        }

        if (parts.Count > HextetCount + 1)
        {
            return false;
        }

        var skipAt = -1;
        for (var index = 1; index < parts.Count - 1; index++)
        {
            if (parts[index] != "")
            {
                continue;
            }

            if (skipAt != -1)
            {
                return false;
            }

            skipAt = index;
        }

        var above = 0;
        var below = 0;
        if (skipAt == -1)
        {
            if (parts.Count != HextetCount || parts[0] == "" || parts[^1] == "")
            {
                return false;
            }

            above = HextetCount;
            below = 0;
        }
        else
        {
            above = skipAt;
            below = parts.Count - skipAt - 1;
            if (parts[0] == "")
            {
                above -= 1;
                if (above != 0)
                {
                    return false;
                }
            }

            if (parts[^1] == "")
            {
                below -= 1;
                if (below != 0)
                {
                    return false;
                }
            }

            if (HextetCount - (above + below) < 1)
            {
                return false;
            }
        }

        var left = parts.Take(above);
        var right = parts.Skip(parts.Count - below);
        return left.Concat(right).All(part => HextetPattern.IsMatch(part));
    }

    private static bool IsBracketedHost(string hostname)
    {
        if (hostname.StartsWith("v", StringComparison.Ordinal))
        {
            return IpvFuturePattern.IsMatch(hostname);
        }

        return IsIpv6Address(hostname);
    }
}
