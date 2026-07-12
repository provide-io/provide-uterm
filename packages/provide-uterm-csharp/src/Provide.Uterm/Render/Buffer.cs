//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Vt;

namespace Provide.Uterm.Render;

public static class RenderBuffer
{
    public const string AnsiReset = "\x1b[0m";

    private static readonly Dictionary<string, int> FgCodes = new(StringComparer.Ordinal)
    {
        ["black"] = 30, ["red"] = 31, ["green"] = 32, ["yellow"] = 33,
        ["blue"] = 34, ["magenta"] = 35, ["cyan"] = 36, ["white"] = 37,
        ["brown"] = 33,
        ["brightblack"] = 90, ["brightred"] = 91, ["brightgreen"] = 92, ["brightyellow"] = 93,
        ["brightblue"] = 94, ["brightmagenta"] = 95, ["brightcyan"] = 96, ["brightwhite"] = 97,
        ["brightbrown"] = 93,
    };

    private static readonly Dictionary<string, int> BgCodes = new(StringComparer.Ordinal)
    {
        ["black"] = 40, ["red"] = 41, ["green"] = 42, ["yellow"] = 43,
        ["blue"] = 44, ["magenta"] = 45, ["cyan"] = 46, ["white"] = 47,
        ["brown"] = 43,
        ["brightblack"] = 100, ["brightred"] = 101, ["brightgreen"] = 102, ["brightyellow"] = 103,
        ["brightblue"] = 104, ["brightmagenta"] = 105, ["brightcyan"] = 106, ["brightwhite"] = 107,
        ["brightbrown"] = 103, ["bfightmagenta"] = 105,
    };

    public struct Style : IEquatable<Style>
    {
        public string FG { get; set; }
        public string BG { get; set; }
        public bool Bold { get; set; }
        public bool Underscore { get; set; }
        public bool Reverse { get; set; }
        public bool Blink { get; set; }

        public readonly bool Equals(Style other) =>
            FG == other.FG && BG == other.BG && Bold == other.Bold &&
            Underscore == other.Underscore && Reverse == other.Reverse && Blink == other.Blink;

        public override readonly bool Equals(object? obj) => obj is Style s && Equals(s);
        public override readonly int GetHashCode() => HashCode.Combine(FG, BG, Bold, Underscore, Reverse, Blink);
        public static bool operator ==(Style a, Style b) => a.Equals(b);
        public static bool operator !=(Style a, Style b) => !a.Equals(b);
    }

    public static Style DefaultStyle { get; } = new() { FG = "default", BG = "default" };

    private static bool IsHexColor(string value)
    {
        if (value.Length != 6)
        {
            return false;
        }

        foreach (var c in value.ToLowerInvariant())
        {
            if (c is (< '0' or > '9') and (< 'a' or > 'f'))
            {
                return false;
            }
        }

        return true;
    }

    private static List<int> ColorSgr(string color, bool isFg)
    {
        if (color == "default")
        {
            return [];
        }

        var table = isFg ? FgCodes : BgCodes;
        var baseCode = isFg ? 38 : 48;
        if (table.TryGetValue(color, out var code))
        {
            return [code];
        }

        if (IsHexColor(color))
        {
            var r = Convert.ToInt32(color[..2], 16);
            var g = Convert.ToInt32(color[2..4], 16);
            var b = Convert.ToInt32(color[4..6], 16);
            return [baseCode, 2, r, g, b];
        }

        return [];
    }

    public static string StyleToSgr(Style s)
    {
        var fg = s.FG;
        var bg = s.BG;
        if (s.Reverse)
        {
            (fg, bg) = (bg, fg);
        }

        var codes = new List<int>();
        if (s.Bold)
        {
            codes.Add(1);
        }

        if (s.Underscore)
        {
            codes.Add(4);
        }

        if (s.Blink)
        {
            codes.Add(5);
        }

        codes.AddRange(ColorSgr(fg, true));
        codes.AddRange(ColorSgr(bg, false));
        if (codes.Count == 0)
        {
            return AnsiReset;
        }

        return "\x1b[" + string.Join(";", codes) + "m";
    }

    public static Style CellStyle(Vt.Char cell)
    {
        var fg = string.IsNullOrEmpty(cell.FG) ? "default" : cell.FG;
        var bg = string.IsNullOrEmpty(cell.BG) ? "default" : cell.BG;
        return new Style
        {
            FG = fg,
            BG = bg,
            Bold = cell.Bold,
            Underscore = cell.Underscore,
            Reverse = cell.Reverse,
            Blink = cell.Blink,
        };
    }

    public static IReadOnlyList<string> RenderScreenLines(Vt.Screen screen, int width, int height)
    {
        var lines = new string[height];
        for (var y = 0; y < height; y++)
        {
            var parts = new StringBuilder();
            var haveLast = false;
            var last = DefaultStyle;
            for (var x = 0; x < width; x++)
            {
                var cell = screen.At(y, x);
                var style = CellStyle(cell);
                var ch = cell.Data;
                if (string.IsNullOrEmpty(ch))
                {
                    ch = " ";
                }

                if (!haveLast || style != last)
                {
                    parts.Append(StyleToSgr(style));
                    last = style;
                    haveLast = true;
                }

                parts.Append(ch);
            }

            parts.Append(AnsiReset);
            lines[y] = parts.ToString();
        }

        return lines;
    }
}
