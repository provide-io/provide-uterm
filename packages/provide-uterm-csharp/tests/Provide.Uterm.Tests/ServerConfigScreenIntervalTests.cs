//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// <c>[recording] screen_interval_s</c> is 0 or a finite number of seconds no
/// shorter than the settle period, refused with the reference's own text.
/// </summary>
public sealed class ServerConfigScreenIntervalTests
{
    private static UtermServerConfig Load(string value)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-screen-interval-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, "[recording]\nscreen_interval_s = " + value + "\n");
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Theory]
    [InlineData("nan", "nan")]
    [InlineData("inf", "inf")]
    [InlineData("-inf", "-inf")]
    [InlineData("0.1", "0.1")]
    [InlineData("-1", "-1.0")]
    public void AnIntervalThatCouldNotMeanWhatItSaysIsRefused(string written, string shown)
    {
        var error = Assert.Throws<ArgumentException>(() => Load(written));
        Assert.Equal(
            "recording.screen_interval_s must be 0 (only screens sent to a viewer) or a finite number of seconds >= 0.25, got: "
            + shown,
            error.Message);
    }

    [Theory]
    [InlineData(1e-5, "1e-05")]
    [InlineData(-1e20, "-1e+20")]
    [InlineData(1.5e16, "1.5e+16")]
    [InlineData(1e16, "1e+16")]
    [InlineData(1234567890123456.0, "1234567890123456.0")]
    [InlineData(0.0001, "0.0001")]
    [InlineData(0.1, "0.1")]
    [InlineData(-1.0, "-1.0")]
    [InlineData(1e15, "1000000000000000.0")]
    [InlineData(100.0, "100.0")]
    [InlineData(10.5, "10.5")]
    [InlineData(0.00501, "0.00501")]
    [InlineData(1.234e-5, "1.234e-05")]
    [InlineData(1.2345678901234568e17, "1.2345678901234568e+17")]
    [InlineData(1e-7, "1e-07")]
    [InlineData(1e23, "1e+23")]
    [InlineData(-0.0, "-0.0")]
    [InlineData(9.999999999999999e-05, "9.999999999999999e-05")]
    [InlineData(9999999999999998.0, "9999999999999998.0")]
    [InlineData(1.0000000000000002e16, "1.0000000000000002e+16")]
    [InlineData(double.NaN, "nan")]
    [InlineData(double.PositiveInfinity, "inf")]
    [InlineData(double.NegativeInfinity, "-inf")]
    public void AFloatPrintsAsPythonsRepr(double value, string expected)
    {
        Assert.Equal(expected, ConfigLoader.PyFloat(value));
    }

    [Theory]
    [InlineData("0", 0.0)]
    [InlineData("0.25", 0.25)]
    [InlineData("1", 1.0)]
    public void AnOffOrSettledIntervalLoads(string written, double expected)
    {
        Assert.Equal(expected, Load(written).Recording.ScreenIntervalS);
    }
}
