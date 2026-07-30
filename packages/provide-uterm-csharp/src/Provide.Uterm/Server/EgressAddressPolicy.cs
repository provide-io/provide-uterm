//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Net;
using System.Net.Sockets;

namespace Provide.Uterm.Server;

/// <summary>
/// IP classification for the egress guard: which addresses an outbound request
/// the server makes on a caller's behalf may never reach.
/// </summary>
/// <remarks>
/// <para>
/// SECURITY-CRITICAL. The CIDR lists below are a <em>literal</em> port of
/// <c>blockedPrivateV4</c> / <c>blockedPrivateV6</c> / <c>metadataIPs</c> in
/// <c>packages/provide-uterm-go/server/server_egress.go</c> (lines 79-113).
/// Go is the reference for the lists specifically, per
/// <c>conformance/EGRESS_GUARD.md</c> §1: the Python reference derives this set
/// from CPython's <c>ipaddress.is_private</c> / <c>is_loopback</c> /
/// <c>is_link_local</c> / <c>is_multicast</c> / <c>is_reserved</c> /
/// <c>is_unspecified</c>, and .NET — like Go — has no stdlib equivalent to lean
/// on. The Go lists were derived to be the exact union of those classifiers on
/// Python 3.13, so they are copied here rather than re-derived; re-deriving them
/// by hand is how a port ends up subtly narrower than the reference.
/// </para>
/// <para>
/// Deliberately absent from <see cref="BlockedPrivateV6"/> are the
/// IPv4-carrying IPv6 forms (<c>::ffff:0:0/96</c> mapped, <c>2002::/16</c> 6to4,
/// <c>64:ff9b::/96</c> NAT64): <see cref="DecodeEmbeddedIPv4"/> rewrites those
/// to the IPv4 they carry <em>before</em> classification, which is the only way
/// <c>64:ff9b::169.254.169.254</c> — a v6 literal that reaches the v4 metadata
/// service on a NAT64 cluster — gets classified as metadata.
/// </para>
/// <para>
/// Shared with <see cref="EgressGuard"/> (the connector/RFB dial guard), which
/// used to carry its own narrower approximation of the same question. That copy
/// was the permissive one — no IPv6 metadata address, no embedded-IPv4 decode, no
/// reserved or documentation ranges — and two classifiers answering one question
/// only ever drift further apart. The connector guard keeps the
/// <c>blockPrivate</c> distinction the reference draws (metadata always, the rest
/// only under the flag), and now shares this list entire.
/// </para>
/// </remarks>
public static class EgressAddressPolicy
{
    /// <summary>
    /// Cloud-metadata addresses, refused unconditionally. Port of
    /// <c>metadataIPs</c> (<c>_net._METADATA_IPS</c> in the reference): the
    /// AWS/GCP/Azure link-local, the Alibaba Cloud metadata IP, and the EC2 IPv6
    /// metadata address. Reaching any of these returns instance role
    /// credentials, which is why no configuration key re-opens them.
    /// </summary>
    private static readonly IPAddress[] MetadataAddresses =
    {
        IPAddress.Parse("169.254.169.254"),
        IPAddress.Parse("100.100.100.200"),
        IPAddress.Parse("fd00:ec2::254"),
    };

    /// <summary>NAT64 well-known prefix, for <see cref="DecodeEmbeddedIPv4"/>.</summary>
    private static readonly Cidr Nat64WellKnown = Cidr.Parse("64:ff9b::/96");

    /// <summary>
    /// Port of Go <c>blockedPrivateV4</c>: the union of CPython's IPv4
    /// <c>is_private</c> networks (which is where <c>0.0.0.0/8</c>,
    /// <c>127.0.0.0/8</c>, <c>169.254.0.0/16</c>, the IETF protocol assignments
    /// <c>192.0.0.0/24</c>, the documentation and benchmarking ranges, and
    /// <c>240.0.0.0/4</c> come from) plus multicast.
    /// </summary>
    private static readonly Cidr[] BlockedPrivateV4 = ParseCidrs(
        "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16",
        "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
        "255.255.255.255/32",
        "224.0.0.0/4", // multicast
        // RFC 6598 carrier-grade NAT. The one entry NOT inherited from CPython's
        // classifiers: `ipaddress.ip_address("100.64.0.1").is_private` is False
        // (gh-113171 moved this block out of `_private_networks`), so the Go
        // union this list ports omits it. That is a gap in the derivation, not
        // an allowance — CGNAT carries real infrastructure on carrier and
        // container networks and is exactly what an SSRF pivot wants.
        //
        // It lived on the connector guard alone until every port blocked it;
        // keeping it there would have left the *webhook* path here permitting
        // an address Python, Go and TypeScript all refuse. See
        // conformance/EGRESS_GUARD.md §1, which now pins it for all four.
        "100.64.0.0/10");

    /// <summary>
    /// Port of Go <c>blockedPrivateV6</c>: CPython's IPv6 <c>is_private</c>
    /// networks, then multicast, then the IANA-reserved blocks that
    /// <c>is_reserved</c> covers.
    /// </summary>
    private static readonly Cidr[] BlockedPrivateV6 = ParseCidrs(
        // is_private
        "::1/128", "::/128", "64:ff9b:1::/48", "100::/64", "2001::/23",
        "2001:db8::/32", "3fff::/20", "fc00::/7", "fe80::/10",
        // is_multicast
        "ff00::/8",
        // is_reserved
        "::/8", "100::/8", "200::/7", "400::/6", "800::/5", "1000::/4", "4000::/3",
        "6000::/3", "8000::/3", "a000::/3", "c000::/3", "e000::/4", "f000::/5",
        "f800::/6", "fe00::/9");

    /// <summary>Is this one of the cloud-metadata addresses? Always refused.</summary>
    public static bool IsMetadata(IPAddress ip)
    {
        foreach (var candidate in MetadataAddresses)
        {
            if (candidate.Equals(ip))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>
    /// Is this address in private, link-local, multicast, unspecified or
    /// IANA-reserved space? Port of Go <c>isBlockedPrivate</c>: an address that
    /// carries an IPv4 (native or IPv4-mapped) is classified against the v4
    /// list, everything else against the v6 list — mirroring Go's
    /// <c>ip.To4() != nil</c> selection.
    /// </summary>
    public static bool IsBlockedPrivate(IPAddress ip)
    {
        var v4 = AsIPv4(ip);
        return v4 is not null
            ? ContainedIn(BlockedPrivateV4, v4.GetAddressBytes())
            : ContainedIn(BlockedPrivateV6, ip.GetAddressBytes());
    }

    /// <summary>Loopback: <c>127.0.0.0/8</c> and <c>::1</c>. The one conditional case (§2).</summary>
    public static bool IsLoopback(IPAddress ip)
    {
        var v4 = AsIPv4(ip);
        return IPAddress.IsLoopback(v4 ?? ip);
    }

    /// <summary>
    /// The IPv4 an IPv6 address carries, or <c>null</c> when it carries none.
    /// Port of Go <c>decodeEmbeddedIPv4</c> / the reference's
    /// <c>egress._decode_embedded_ipv4</c>.
    /// </summary>
    /// <remarks>
    /// Handles IPv4-mapped (<c>::ffff:a.b.c.d</c>), 6to4 (<c>2002::/16</c>),
    /// the NAT64 well-known prefix (<c>64:ff9b::/96</c>) and the deprecated
    /// IPv4-compatible form (<c>::a.b.c.d</c>). <c>::</c> and <c>::1</c> are
    /// excluded from the last of those and left to the normal v6 branches,
    /// exactly as the Go original does — decoding <c>::1</c> to
    /// <c>0.0.0.1</c> would turn loopback into "some reserved v4 address" and
    /// lose the one classification that is configurable.
    /// </remarks>
    public static IPAddress? DecodeEmbeddedIPv4(IPAddress ip)
    {
        if (ip.AddressFamily == AddressFamily.InterNetwork)
        {
            return ip;
        }

        // Everything past here is IPv6: an IPAddress is one family or the other,
        // so there is no third case to defend against (and no way to write a test
        // that reaches one).
        if (ip.IsIPv4MappedToIPv6)
        {
            return ip.MapToIPv4();
        }

        var b = ip.GetAddressBytes();
        if (b[0] == 0x20 && b[1] == 0x02)
        {
            // 6to4 2002::/16 carries the v4 in the next four bytes.
            return new IPAddress(new[] { b[2], b[3], b[4], b[5] });
        }

        if (Nat64WellKnown.Contains(b))
        {
            return new IPAddress(new[] { b[12], b[13], b[14], b[15] });
        }

        if (IsZeroPrefix(b, 12))
        {
            if (b[12] == 0 && b[13] == 0 && b[14] == 0 && (b[15] == 0 || b[15] == 1))
            {
                // :: and ::1 — handled by the v6 branches.
                return null;
            }

            return new IPAddress(new[] { b[12], b[13], b[14], b[15] });
        }

        return null;
    }

    /// <summary>The IPv4 form of <paramref name="ip"/>, or null when it has none.</summary>
    private static IPAddress? AsIPv4(IPAddress ip) => ip.AddressFamily switch
    {
        AddressFamily.InterNetwork => ip,
        AddressFamily.InterNetworkV6 when ip.IsIPv4MappedToIPv6 => ip.MapToIPv4(),
        _ => null,
    };

    private static bool IsZeroPrefix(byte[] bytes, int count)
    {
        for (var i = 0; i < count; i++)
        {
            if (bytes[i] != 0)
            {
                return false;
            }
        }

        return true;
    }

    private static bool ContainedIn(Cidr[] cidrs, byte[] address)
    {
        foreach (var cidr in cidrs)
        {
            if (cidr.Contains(address))
            {
                return true;
            }
        }

        return false;
    }

    private static Cidr[] ParseCidrs(params string[] cidrs)
    {
        var parsed = new Cidr[cidrs.Length];
        for (var i = 0; i < cidrs.Length; i++)
        {
            parsed[i] = Cidr.Parse(cidrs[i]);
        }

        return parsed;
    }

    /// <summary>
    /// A parsed CIDR. .NET has no <c>IPNetwork.Contains</c> equivalent that
    /// covers both families the way <c>net.IPNet</c> does, so the prefix compare
    /// is spelled out: whole bytes first, then the partial byte under a mask.
    /// </summary>
    private readonly struct Cidr
    {
        private readonly byte[] _network;
        private readonly int _prefix;

        private Cidr(byte[] network, int prefix)
        {
            _network = network;
            _prefix = prefix;
        }

        internal static Cidr Parse(string cidr)
        {
            var slash = cidr.IndexOf('/', StringComparison.Ordinal);
            var network = IPAddress.Parse(cidr[..slash]);
            var prefix = int.Parse(cidr[(slash + 1)..], CultureInfo.InvariantCulture);
            return new Cidr(network.GetAddressBytes(), prefix);
        }

        internal bool Contains(byte[] address)
        {
            if (address.Length != _network.Length)
            {
                return false;
            }

            var wholeBytes = _prefix / 8;
            for (var i = 0; i < wholeBytes; i++)
            {
                if (address[i] != _network[i])
                {
                    return false;
                }
            }

            var remainingBits = _prefix % 8;
            if (remainingBits == 0)
            {
                return true;
            }

            var mask = (byte)(0xFF << (8 - remainingBits));
            return (address[wholeBytes] & mask) == (_network[wholeBytes] & mask);
        }
    }
}
