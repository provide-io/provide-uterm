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
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("127.0.0.1", blockPrivate: true));
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
    [InlineData("100.64.0.1")]
    [InlineData("0.0.0.1")]
    [InlineData("224.0.0.1")]
    [InlineData("169.254.1.1")]
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
        // Public documentation address 2001:db8::1 is not private/link-local.
        await EgressGuard.AssertConnectorTargetAllowedAsync("[2001:db8::1]", blockPrivate: true);
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
