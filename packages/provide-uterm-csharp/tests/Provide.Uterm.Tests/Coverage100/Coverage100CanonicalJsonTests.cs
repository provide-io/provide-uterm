//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using Provide.Uterm.CtrlMsg;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The shortest-scientific step re-spells the round-trip "R" digits; it never
/// needs a second, higher-precision attempt. That fallback was removed, so the
/// invariant that made it dead is pinned here instead.
/// </summary>
public sealed class Coverage100CanonicalJsonTests
{
    [Fact]
    public void Py_float_repr_round_trips_every_sampled_finite_double_bit_exactly()
    {
        var rng = new Random(20261011);
        var buf = new byte[8];
        var checkedCount = 0;
        for (var i = 0; i < 200_000; i++)
        {
            rng.NextBytes(buf);
            var f = BitConverter.ToDouble(buf);
            if (!double.IsFinite(f))
            {
                continue;
            }

            AssertRoundTrips(f);
            checkedCount++;
        }

        Assert.True(checkedCount > 190_000);
    }

    [Theory]
    [InlineData(100.0)]
    [InlineData(1e15)]
    [InlineData(1e16)]
    [InlineData(123456789012345680.0)]
    [InlineData(0.000123)]
    [InlineData(-2.5e-7)]
    [InlineData(5e-324)]
    [InlineData(double.MaxValue)]
    [InlineData(-0.1)]
    public void Py_float_repr_round_trips_boundary_values(double f) => AssertRoundTrips(f);

    private static void AssertRoundTrips(double f)
    {
        var repr = CanonicalJson.PyFloatRepr(f);
        Assert.True(double.TryParse(repr, NumberStyles.Float, CultureInfo.InvariantCulture, out var back), repr);
        Assert.Equal(BitConverter.DoubleToInt64Bits(f), BitConverter.DoubleToInt64Bits(back));
    }
}
