//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Ansi;
using Provide.Uterm.Colors;

namespace Provide.Uterm.Tests;

public class AnsiColorsTests
{
    [Fact]
    public void UpgradeTo256_ConvertsBasicSgr()
    {
        var input = "\x1b[31mred\x1b[0m";
        var out256 = Upgrade.UpgradeTo256(input);
        Assert.Contains("38;5;", out256, StringComparison.Ordinal);
        Assert.Contains("red", out256, StringComparison.Ordinal);
    }

    [Fact]
    public void UpgradeToTruecolor_ConvertsBasicSgr()
    {
        var input = "\x1b[32mgreen\x1b[0m";
        var outTc = Upgrade.UpgradeToTruecolor(input);
        Assert.Contains("38;2;", outTc, StringComparison.Ordinal);
        Assert.Contains("green", outTc, StringComparison.Ordinal);
    }

    [Fact]
    public void Upgrade_Tokens_And_Bg()
    {
        var tok = Upgrade.UpgradeTo256("{P1}{T2}");
        Assert.Contains("{F", tok, StringComparison.Ordinal);
        Assert.Contains("{B", tok, StringComparison.Ordinal);

        var bg = Upgrade.UpgradeTo256("\x1b[41mBG\x1b[0m");
        Assert.Contains("48;5;", bg, StringComparison.Ordinal);

        var tcTok = Upgrade.UpgradeToTruecolor("{P0}");
        Assert.Contains("38;2;", tcTok, StringComparison.Ordinal);
    }

    [Fact]
    public void Upgrade_LeavesExtendedSgrAlone()
    {
        var input = "\x1b[38;5;196mX\x1b[0m";
        Assert.Equal(input, Upgrade.UpgradeTo256(input));
    }

    [Fact]
    public void Downgrade_TruecolorTo256And16()
    {
        var truecolor = "\x1b[38;2;255;0;0mR\x1b[0m";
        var d256 = Downgrade.DowngradeTo256(truecolor);
        Assert.Contains("38;5;", d256, StringComparison.Ordinal);
        Assert.DoesNotContain("38;2;", d256, StringComparison.Ordinal);

        var d16 = Downgrade.DowngradeTo16(truecolor);
        Assert.DoesNotContain("38;2;", d16, StringComparison.Ordinal);
        Assert.Contains("m", d16, StringComparison.Ordinal);
    }

    [Fact]
    public void ColorModes_ParseAndApply()
    {
        Assert.Equal(ColorMode.Passthrough, ColorModes.Parse(ColorModes.Passthrough));
        Assert.Equal(ColorMode.Mode256, ColorModes.Parse(ColorModes.Mode256));
        Assert.Equal(ColorMode.Mode16, ColorModes.Parse("16"));
        Assert.Equal(ColorMode.Mode16, ColorModes.Parse("other"));

        var data = "\x1b[38;2;10;20;30mX\x1b[0m";
        Assert.Equal(data, ColorModes.ApplyColorMode(data, ColorMode.Passthrough));
        Assert.Contains("38;5;", ColorModes.ApplyColorMode(data, ColorMode.Mode256), StringComparison.Ordinal);
        Assert.DoesNotContain("38;2;", ColorModes.ApplyColorMode(data, "16"), StringComparison.Ordinal);
    }

    [Fact]
    public void ColorModes_ApplyBytes()
    {
        var latin1 = Encoding.GetEncoding("ISO-8859-1");
        var data = latin1.GetBytes("\x1b[38;2;255;128;0mZ\x1b[0m");
        var passthrough = ColorModes.ApplyColorModeBytes(data, ColorMode.Passthrough);
        Assert.Equal(data, passthrough);

        var d256 = ColorModes.ApplyColorModeBytes(data, ColorMode.Mode256);
        Assert.Contains("38;5;", latin1.GetString(d256), StringComparison.Ordinal);

        var d16 = ColorModes.ApplyColorModeBytes(data, "16");
        Assert.DoesNotContain("38;2;", latin1.GetString(d16), StringComparison.Ordinal);
    }

    [Fact]
    public void Rgb_To256_And_To16()
    {
        Assert.Equal(16, Rgb.RgbTo256(0, 0, 0));
        Assert.Equal(231, Rgb.RgbTo256(255, 255, 255));
        var mid = Rgb.RgbTo256(128, 128, 128);
        Assert.InRange(mid, 232, 255);

        var cube = Rgb.RgbTo256(255, 0, 0);
        Assert.InRange(cube, 16, 231);

        Assert.Equal(0, Rgb.RgbTo16Index(0, 0, 0));
        Assert.Equal(15, Rgb.RgbTo16Index(255, 255, 255));
        Assert.InRange(Rgb.RgbTo16Index(255, 0, 0), 0, 15);
    }

    [Fact]
    public void AnsiConstants_Present()
    {
        Assert.Equal(16, AnsiConstants.DefaultPalette.Length);
        Assert.Equal(16, AnsiConstants.DefaultRgb.Length);
        Assert.Contains("[2J", AnsiConstants.ClearScreen, StringComparison.Ordinal);
        Assert.Equal("\x1b[0m", AnsiConstants.Reset);
    }

    [Fact]
    public void Sgr_RewriteParams_Empty()
    {
        Assert.Equal("\x1b[m", Sgr.RewriteParams("", ColorMode.Mode16));
        Assert.Equal("\x1b[1m", Sgr.RewriteParams("1", ColorMode.Mode16));
    }
}
