//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Colors;

/// <summary>RGB-to-palette mapping for color downgrade.</summary>
public static class Rgb
{
    private static readonly (int R, int G, int B)[] Palette16 =
    [
        (0, 0, 0), (0, 0, 205), (0, 205, 0), (0, 205, 205),
        (205, 0, 0), (205, 0, 205), (205, 205, 0), (229, 229, 229),
        (127, 127, 127), (92, 92, 255), (92, 255, 92), (92, 255, 255),
        (255, 92, 92), (255, 92, 255), (255, 255, 92), (255, 255, 255),
    ];

    private static int Clamp8(int v) => v < 0 ? 0 : v > 255 ? 255 : v;

    /// <summary>Banker's rounding like Python round() / math.RoundToEven.</summary>
    private static int RoundToEven(double x) => (int)Math.Round(x, MidpointRounding.ToEven);

    public static int RgbTo256(int r, int g, int b)
    {
        var rr = Clamp8(r);
        var gg = Clamp8(g);
        var bb = Clamp8(b);
        if (rr == gg && gg == bb)
        {
            if (rr < 8)
            {
                return 16;
            }

            if (rr > 248)
            {
                return 231;
            }

            return 232 + (int)((rr - 8) / 247.0 * 24);
        }

        var rc = RoundToEven(rr / 255.0 * 5);
        var gc = RoundToEven(gg / 255.0 * 5);
        var bc = RoundToEven(bb / 255.0 * 5);
        return 16 + 36 * rc + 6 * gc + bc;
    }

    public static int RgbTo16Index(int r, int g, int b)
    {
        var rr = Clamp8(r);
        var gg = Clamp8(g);
        var bb = Clamp8(b);
        var bestI = 0;
        var bestD = int.MaxValue;
        for (var i = 0; i < 16; i++)
        {
            var (tr, tg, tb) = Palette16[i];
            var d = (rr - tr) * (rr - tr) + (gg - tg) * (gg - tg) + (bb - tb) * (bb - tb);
            if (d < bestD)
            {
                bestD = d;
                bestI = i;
            }
        }

        return bestI;
    }
}
