//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Ansi;

public readonly struct RgbColor
{
    public int R { get; init; }
    public int G { get; init; }
    public int B { get; init; }

    public RgbColor(int r, int g, int b)
    {
        R = r;
        G = g;
        B = b;
    }
}

public static class AnsiConstants
{
    public static readonly int[] DefaultPalette =
    [
        0, 160, 34, 184, 27, 127, 37, 252,
        244, 196, 46, 226, 51, 201, 87, 231,
    ];

    public static readonly RgbColor[] DefaultRgb =
    [
        new(0, 0, 0), new(215, 0, 0), new(0, 175, 0), new(215, 175, 0),
        new(0, 95, 255), new(175, 0, 175), new(0, 175, 175), new(208, 208, 208),
        new(128, 128, 128), new(255, 0, 0), new(0, 255, 0), new(255, 255, 0),
        new(0, 175, 255), new(255, 0, 255), new(95, 255, 255), new(255, 255, 255),
    ];

    public const string ClearScreen = "\x1b[2J\x1b[H";
    public const string Bold = "\x1b[1m";
    public const string Reset = "\x1b[0m";

    private static readonly int[] CubeLevels = [0, 95, 135, 175, 215, 255];

    internal static RgbColor Color256ToRgb(int idx)
    {
        if (idx < 16)
        {
            return DefaultRgb[idx];
        }

        if (idx < 232)
        {
            idx -= 16;
            var b = idx % 6;
            idx /= 6;
            var g = idx % 6;
            var r = idx / 6;
            return new RgbColor(CubeLevels[r], CubeLevels[g], CubeLevels[b]);
        }

        var gray = 8 + (idx - 232) * 10;
        return new RgbColor(gray, gray, gray);
    }

    internal static RgbColor[] PaletteToRgb(int[] palette) =>
        palette.Select(Color256ToRgb).ToArray();

    internal static bool MapIndex(int code, out int idx)
    {
        switch (code)
        {
            case >= 30 and <= 37:
                idx = code - 30;
                return true;
            case >= 90 and <= 97:
                idx = 8 + (code - 90);
                return true;
            case >= 40 and <= 47:
                idx = code - 40;
                return true;
            case >= 100 and <= 107:
                idx = 8 + (code - 100);
                return true;
            default:
                idx = 0;
                return false;
        }
    }
}
