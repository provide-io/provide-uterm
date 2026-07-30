//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using Provide.Uterm.Server;
using Xunit;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// Direct unit coverage for <see cref="EgressAddressPolicy"/>'s boundary logic
/// that the HTTP-level webhook/connector guard suites do not reach byte-for-byte
/// — the zero-prefix decode inside <see cref="EgressAddressPolicy.DecodeEmbeddedIPv4"/>
/// and the IPv4-mapped loopback case in <see cref="EgressAddressPolicy.IsLoopback"/>.
/// </summary>
public sealed class EgressAddressPolicyTests
{
    [Fact]
    public void AnIPv4MappedLoopbackAddressIsLoopback()
    {
        // IsLoopback maps v4-mapped v6 addresses to their v4 form before asking
        // .NET's own IsLoopback — dropping that mapping (`v4 ?? ip` losing its
        // left side) would ask IsLoopback about the raw wrapper form instead.
        Assert.True(EgressAddressPolicy.IsLoopback(IPAddress.Parse("::ffff:127.0.0.1")));
    }

    // The zero-prefix branch of DecodeEmbeddedIPv4 is
    //   b[12]==0 && b[13]==0 && b[14]==0 && (b[15]==0 || b[15]==1)
    // guarding "this is :: or ::1, leave it to the ordinary v6 branches" (null)
    // versus "this is some other ::a.b.c.d, decode it" (non-null). Each case
    // below pins one join in that chain by making exactly one earlier byte
    // nonzero, so a mutant that loosens any `&&` to `||` (or flips the trailing
    // equality) returns null where the real embedded address must come back.
    [Theory]
    [InlineData("0:0:0:0:0:0:5:0", 0, 5, 0, 0)] // b13 nonzero: pins the b12/b13 join
    [InlineData("0:0:0:0:0:0:0:700", 0, 0, 7, 0)] // b14 nonzero: pins the b13/b14 join
    [InlineData("0:0:0:0:0:0:0:2", 0, 0, 0, 2)] // b15==2: pins the trailing (==0||==1)
    public void AnAlmostZeroEmbeddedAddressIsDecodedRatherThanTreatedAsUnspecified(
        string address, byte b12, byte b13, byte b14, byte b15)
    {
        var decoded = EgressAddressPolicy.DecodeEmbeddedIPv4(IPAddress.Parse(address));

        Assert.NotNull(decoded);
        Assert.Equal(new IPAddress(new[] { b12, b13, b14, b15 }), decoded);
    }

    [Theory]
    [InlineData("::")]
    [InlineData("::1")]
    public void TrueUnspecifiedAndLoopbackAreLeftToTheOrdinaryV6Branches(string address)
    {
        // The one case the zero-prefix branch must still return null for: :: and
        // ::1 keep their ordinary v6 classification rather than decoding to
        // 0.0.0.0 / 0.0.0.1.
        Assert.Null(EgressAddressPolicy.DecodeEmbeddedIPv4(IPAddress.Parse(address)));
    }
}
