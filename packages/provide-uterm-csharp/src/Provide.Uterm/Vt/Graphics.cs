//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

internal static class Graphics
{
    internal static readonly Dictionary<int, string> FgAnsi = new()
    {
        [30] = "black", [31] = "red", [32] = "green", [33] = "brown",
        [34] = "blue", [35] = "magenta", [36] = "cyan", [37] = "white",
        [39] = "default",
    };

    internal static readonly Dictionary<int, string> BgAnsi = new()
    {
        [40] = "black", [41] = "red", [42] = "green", [43] = "brown",
        [44] = "blue", [45] = "magenta", [46] = "cyan", [47] = "white",
        [49] = "default",
    };

    internal static readonly Dictionary<int, string> FgAixTerm = new()
    {
        [90] = "brightblack", [91] = "brightred", [92] = "brightgreen", [93] = "brightbrown",
        [94] = "brightblue", [95] = "brightmagenta", [96] = "brightcyan", [97] = "brightwhite",
    };

    // "bfightmagenta" reproduces a pyte.graphics.BG_AIXTERM typo intentionally.
    internal static readonly Dictionary<int, string> BgAixTerm = new()
    {
        [100] = "brightblack", [101] = "brightred", [102] = "brightgreen", [103] = "brightbrown",
        [104] = "brightblue", [105] = "bfightmagenta", [106] = "brightcyan", [107] = "brightwhite",
    };

    internal static readonly string[] FgBg256 = MakeFgBg256();

    private static string[] MakeFgBg256()
    {
        var rgb = new List<(int, int, int)>
        {
            (0x00, 0x00, 0x00), (0xcd, 0x00, 0x00), (0x00, 0xcd, 0x00), (0xcd, 0xcd, 0x00),
            (0x00, 0x00, 0xee), (0xcd, 0x00, 0xcd), (0x00, 0xcd, 0xcd), (0xe5, 0xe5, 0xe5),
            (0x7f, 0x7f, 0x7f), (0xff, 0x00, 0x00), (0x00, 0xff, 0x00), (0xff, 0xff, 0x00),
            (0x5c, 0x5c, 0xff), (0xff, 0x00, 0xff), (0x00, 0xff, 0xff), (0xff, 0xff, 0xff),
        };
        int[] steps = [0x00, 0x5f, 0x87, 0xaf, 0xd7, 0xff];
        for (var i = 0; i < 216; i++)
        {
            rgb.Add((steps[(i / 36) % 6], steps[(i / 6) % 6], steps[i % 6]));
        }

        for (var i = 0; i < 24; i++)
        {
            var v = 8 + i * 10;
            rgb.Add((v, v, v));
        }

        return rgb.Select(c => $"{c.Item1:x2}{c.Item2:x2}{c.Item3:x2}").ToArray();
    }
}
