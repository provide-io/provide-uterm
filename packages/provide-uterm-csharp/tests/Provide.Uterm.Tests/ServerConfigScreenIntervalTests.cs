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
    [InlineData("0", 0.0)]
    [InlineData("0.25", 0.25)]
    [InlineData("1", 1.0)]
    public void AnOffOrSettledIntervalLoads(string written, double expected)
    {
        Assert.Equal(expected, Load(written).Recording.ScreenIntervalS);
    }
}
