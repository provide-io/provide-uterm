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
        public bool Italics { get; set; }
        public bool Strikethrough { get; set; }

        public readonly bool Equals(Style other) =>
            FG == other.FG && BG == other.BG && Bold == other.Bold &&
            Underscore == other.Underscore && Reverse == other.Reverse && Blink == other.Blink &&
            Italics == other.Italics && Strikethrough == other.Strikethrough;

        public override readonly bool Equals(object? obj) => obj is Style s && Equals(s);
        public override readonly int GetHashCode() => HashCode.Combine(FG, BG, Bold, Underscore, Reverse, Blink, Italics, Strikethrough);
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

    internal static List<int> ColorSgr(string color, bool isFg)
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

    /// <summary>
    /// Convert cell style attributes to an SGR escape sequence.
    /// </summary>
    /// <remarks>
    /// The sequence is absolute: it starts from a reset, so it sets exactly this
    /// style whatever the previous cell's was (relative to it, a cell that
    /// dropped an attribute kept it, and a background bled along the row).
    /// Reverse video is SGR 7: swapping the colours instead is no change at all
    /// when both are the default, and the reversed cell vanished. The attributes
    /// go in the order the pen's do (1, 3, 4, 5, 7, 9).
    /// </remarks>
    public static string StyleToSgr(Style s)
    {
        var codes = new List<int> { 0 };
        if (s.Bold)
        {
            codes.Add(1);
        }

        if (s.Italics)
        {
            codes.Add(3);
        }

        if (s.Underscore)
        {
            codes.Add(4);
        }

        if (s.Blink)
        {
            codes.Add(5);
        }

        if (s.Reverse)
        {
            codes.Add(7);
        }

        if (s.Strikethrough)
        {
            codes.Add(9);
        }

        codes.AddRange(ColorSgr(s.FG, true));
        codes.AddRange(ColorSgr(s.BG, false));
        if (codes.Count == 1)
        {
            return AnsiReset;
        }

        return SgrSequence(codes);
    }

    internal static string SgrSequence(List<int> codes) => "\x1b[" + string.Join(";", codes) + "m";

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
            Italics = cell.Italics,
            Strikethrough = cell.Strikethrough,
        };
    }

    /// <summary>
    /// Drawn where a cell holds the empty second half of a wide character whose
    /// first half is gone, to recreate that cell: see <see cref="RenderRow"/>.
    /// Any wide character would do; this one is blank should a terminal ever
    /// show it.
    /// </summary>
    private const string Placeholder = "\u3000";

    /// <summary>
    /// Render the screen's rows with SGR escapes wherever the style changes
    /// between adjacent cells. Drawn on fresh rows, the rows recreate the
    /// cells: see <see cref="RenderRow"/>.
    /// </summary>
    public static IReadOnlyList<string> RenderScreenLines(Vt.Screen screen, int width, int height)
    {
        var lines = new string[height];
        for (var y = 0; y < height; y++)
        {
            lines[y] = RenderRow(screen, y, width);
        }

        return lines;
    }

    /// <summary>The width of the first code point of a cell's text, which must not be empty.</summary>
    private static int FirstWidth(string text) => Wcwidth.RuneWidth(char.ConvertToUtf32(text, 0));

    /// <summary>Whether a cell holds what a wide character's second cell does: nothing, or marks.</summary>
    internal static bool IsSecondHalf(string text) => text.Length == 0 || FirstWidth(text) == 0;

    /// <summary>
    /// Render row <paramref name="y"/> so that drawing it on a fresh row
    /// recreates every cell. Port of render/buffer.py <c>_render_row</c>; every
    /// port writes the same bytes.
    /// </summary>
    /// <remarks>
    /// A wide character is drawn once and takes two cells; the cell after it is
    /// recreated by drawing it, so nothing is written for that cell. Writing a
    /// space there, as this once did, made every wide character a column wider
    /// on the terminal it was drawn on, and a row holding one overflowed.
    /// Combining marks that joined that second cell are written straight after
    /// the wide character, which joins them to it again; marks joined to the wide
    /// character itself are written with the column set back onto its second
    /// cell, which joins them to the wide character instead.
    ///
    /// Two cells cannot be recreated by drawing left to right, and get a cursor
    /// move. A wide character whose second half was overwritten is drawn, and
    /// the column is then set back onto that half (CHA, not CUB: from the
    /// deferred wrap at the last column CUB steps one further). A second half
    /// whose first half is gone (overwritten, or deleted) is recreated the way
    /// the stream made it: a placeholder wide character drawn in its style, its
    /// marks joined to it, and the placeholder's first half deleted (DCH). In
    /// the last column, where a wide character has no second half, the
    /// placeholder is drawn one column left instead, and the cell there drawn
    /// again over its first half. That needs the cell before it to be drawable,
    /// so one arrangement is left blank: such a cell in the last column right
    /// after another second half (a wide character's, or another such cell).
    /// Any marks in it are lost with it.
    ///
    /// A cell with marks written into the last column is written with autowrap
    /// off: the marks join the cell before the cursor, which then rests past the
    /// last column, and with autowrap on they would first wrap to the next row.
    /// </remarks>
    private static string RenderRow(Vt.Screen screen, int y, int cols)
    {
        var parts = new StringBuilder();
        var haveLast = false;
        var last = DefaultStyle;

        void Put(string text, Style style)
        {
            if (!haveLast || style != last)
            {
                parts.Append(StyleToSgr(style));
                last = style;
                haveLast = true;
            }

            parts.Append(text);
        }

        void PutCell(string text, Style style, int x)
        {
            if (text.Length == 0)
            {
                return;
            }

            if (x == cols - 1 && (text.EnumerateRunes().Count() > 1 || FirstWidth(text) == 0))
            {
                parts.Append("\x1b[?7l");
                Put(text, style);
                parts.Append("\x1b[?7h");
            }
            else
            {
                Put(text, style);
            }
        }

        var x = 0;
        while (x < cols)
        {
            var cell = screen.At(y, x);
            var data = cell.Data;
            var style = CellStyle(cell);
            if (IsSecondHalf(data))
            {
                if (x + 1 < cols)
                {
                    Put(Placeholder, style);
                    PutCell(data, style, x + 1);
                    parts.Append($"\x1b[{x + 1}G\x1b[P\x1b[C");
                }
                else if (x > 0)
                {
                    var before = screen.At(y, x - 1);
                    if (!IsSecondHalf(before.Data))
                    {
                        parts.Append($"\x1b[{x}G");
                        Put(Placeholder, style);
                        PutCell(data, style, x);
                        parts.Append($"\x1b[{x}G");
                        PutCell(before.Data, CellStyle(before), x - 1);
                    }
                }

                x++;
                continue;
            }

            if (x + 1 == cols || FirstWidth(data) != 2)
            {
                PutCell(data, style, x);
                x++;
                continue;
            }

            // A wide character with a cell after it. Its own marks are written
            // with the cursor back on that cell, which joins them to the wide
            // character rather than to it.
            var baseLength = char.IsSurrogatePair(data, 0) ? 2 : 1;
            var own = data[baseLength..];
            Put(data[..baseLength], style);
            var after = screen.At(y, x + 1);
            if (IsSecondHalf(after.Data))
            {
                PutCell(after.Data, CellStyle(after), x + 1);
                if (own.Length > 0)
                {
                    parts.Append($"\x1b[{x + 2}G");
                    Put(own, style);
                    if (x + 2 < cols)
                    {
                        parts.Append($"\x1b[{x + 3}G");
                    }
                }

                x += 2;
            }
            else
            {
                parts.Append($"\x1b[{x + 2}G");
                if (own.Length > 0)
                {
                    Put(own, style);
                }

                x++;
            }
        }

        parts.Append(AnsiReset);
        return parts.ToString();
    }
}
