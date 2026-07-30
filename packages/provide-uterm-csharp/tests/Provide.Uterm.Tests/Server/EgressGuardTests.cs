//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Server;
using Xunit;

namespace Provide.Uterm.Tests.Server;

public class EgressGuardTests
{
    [Fact]
    public async Task Metadata_Ip_Always_Blocked()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("169.254.169.254", blockPrivate: false));
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("100.100.100.200", blockPrivate: false));
    }

    [Fact]
    public async Task Loopback_Allowed_When_BlockPrivate_False()
    {
        await EgressGuard.AssertConnectorTargetAllowedAsync("127.0.0.1", blockPrivate: false);
        await EgressGuard.AssertConnectorTargetAllowedAsync("::1", blockPrivate: false);
    }

    [Fact]
    public async Task Loopback_Blocked_When_BlockPrivate_True()
    {
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("127.0.0.1", blockPrivate: true));

        // The exception type alone does not distinguish this refusal from the
        // metadata one above or the unresolvable-host one below; the message is
        // the only thing that says *which* guard fired, and an operator reads it
        // to find out why a connector was refused.
        Assert.Contains("127.0.0.1", ex.Message, StringComparison.Ordinal);
        Assert.Contains("blocked internal address", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Empty_Host_Rejected()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("  ", blockPrivate: false));
    }

    [Theory]
    [InlineData("10.0.0.1")]
    [InlineData("172.16.5.1")]
    [InlineData("172.31.255.1")]
    [InlineData("192.168.1.1")]
    // 100.64.0.1 is RFC 6598 Shared Address Space, and it is the one row here
    // that the shared classifier does not answer: current CPython reports it as
    // neither private nor reserved (gh-113171 moved the block out of
    // `_private_networks`), so the canonical CIDR union §1 pins does not contain
    // it either. It stays refused because the connector guard refused it before
    // the classifiers were consolidated — see EgressGuard.IsSharedAddressSpace
    // for the reasoning and the deliberate, documented divergence.
    [InlineData("100.64.0.1")]
    [InlineData("0.0.0.1")]
    [InlineData("224.0.0.1")]
    [InlineData("169.254.1.1")]
    // 198.18.0.1 (benchmarking) and 203.0.113.5 (documentation) are IANA-reserved
    // space the hand-rolled list knew nothing about; CPython classifies both as
    // private, so the reference refuses them under block_private.
    [InlineData("198.18.0.1")]
    [InlineData("203.0.113.5")]
    public async Task Private_And_Special_Ipv4_Blocked_When_Enabled(string ip)
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync(ip, blockPrivate: true));
    }

    [Theory]
    [InlineData("10.0.0.1")]
    [InlineData("192.168.0.2")]
    public async Task Private_Ipv4_Allowed_When_BlockPrivate_False(string ip)
    {
        await EgressGuard.AssertConnectorTargetAllowedAsync(ip, blockPrivate: false);
    }

    [Fact]
    public async Task Bracketed_Ipv6_Literal_Parsed()
    {
        // The bracket stripping is what this row is really about, so it needs an
        // address whose verdict is unambiguous under the permissive posture.
        await EgressGuard.AssertConnectorTargetAllowedAsync("[2606:4700::1111]", blockPrivate: false);
    }

    [Fact]
    public async Task Ipv6_Documentation_Range_Blocked_When_Enabled()
    {
        // 2001:db8::/32 is the IPv6 documentation range, and it USED to be
        // asserted here as *permitted* under blockPrivate: true — "not
        // private/link-local". That expectation was looser than the reference.
        //
        // `_check_resolved_ip` in
        // packages/provide-uterm-server/src/provide/uterm/server/egress.py
        // refuses an address when block_private is set and any of
        // `ip.is_private or ip.is_loopback or ip.is_link_local or
        // ip.is_multicast or ip.is_unspecified or ip.is_reserved` holds, and
        // CPython's `ipaddress` puts 2001:db8::/32 inside `is_private` (it is in
        // the IPv6 special-purpose registry). So the hosted posture refuses this
        // range for connector targets, and the C# connector guard's hand-rolled
        // range list — which had no notion of it — was the divergence, not the
        // CPython-derived list it has now been pointed at.
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("2001:db8::1", blockPrivate: true));
    }

    [Fact]
    public async Task Ipv6_Documentation_Range_Allowed_When_BlockPrivate_False()
    {
        // The other half, and the whole point of the flag: the reference refuses
        // that set *only* when block_private is true. A connector guard that
        // refused it regardless would break the default posture, under which
        // connectors are supposed to be able to reach internal hosts — that is
        // what they are for.
        await EgressGuard.AssertConnectorTargetAllowedAsync("2001:db8::1", blockPrivate: false);
    }

    [Fact]
    public async Task Ipv6_Metadata_Address_Blocked_Even_When_BlockPrivate_False()
    {
        // fd00:ec2::254 is the EC2 IPv6 metadata address. The connector guard's
        // own metadata list held only the two IPv4 ones, so under the permissive
        // posture this reached the metadata service — and the permissive posture
        // is the default. Cloud metadata is refused unconditionally in the
        // reference (`_METADATA_IPS`, checked before the block_private branch).
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("fd00:ec2::254", blockPrivate: false));
    }

    [Theory]
    [InlineData("64:ff9b::169.254.169.254")]
    [InlineData("2002:a9fe:a9fe::")]
    [InlineData("::ffff:169.254.169.254")]
    [InlineData("::169.254.169.254")]
    public async Task Embedded_Ipv4_Metadata_Blocked_Even_When_BlockPrivate_False(string host)
    {
        // NAT64, 6to4, IPv4-mapped and the deprecated IPv4-compatible form. The
        // reference decodes these before classifying (`_decode_embedded_ipv4`,
        // called from `_check_resolved_ip`) precisely because on a NAT64 cluster
        // the first of them reaches the v4 metadata service. The connector guard
        // did not decode at all, so every one of these was a wrapper it read as
        // "some IPv6 address" and waved through.
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync(host, blockPrivate: false));
    }

    [Fact]
    public async Task Ipv6_Link_Local_Blocked()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("fe80::1", blockPrivate: true));
    }

    [Fact]
    public async Task Ipv6_Unique_Local_Blocked()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("fc00::1", blockPrivate: true));
    }

    [Fact]
    public async Task Hostname_Localhost_Resolves_And_Honors_BlockPrivate()
    {
        await EgressGuard.AssertConnectorTargetAllowedAsync("localhost", blockPrivate: false);
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("localhost", blockPrivate: true));
    }

    [Fact]
    public async Task Unresolvable_Hostname_Fails()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync(
                "no-such-host.invalid." + Guid.NewGuid().ToString("N"),
                blockPrivate: false));
    }

    [Fact]
    public async Task Public_Ipv4_Allowed()
    {
        await EgressGuard.AssertConnectorTargetAllowedAsync("8.8.8.8", blockPrivate: true);
    }
}
