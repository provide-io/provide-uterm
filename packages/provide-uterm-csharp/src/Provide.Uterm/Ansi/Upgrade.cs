//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Ansi;

public static partial class Upgrade
{
    [GeneratedRegex(@"\x1b\[([0-9;]*)m")]
    private static partial Regex SgrRe();

    [GeneratedRegex(@"\{([PT])(\d{1,3})\}")]
    private static partial Regex TokenRe();

    private static string NormalizeDigits(string p)
    {
        var trimmed = p.TrimStart('0');
        return trimmed.Length == 0 ? "0" : trimmed;
    }

    private static string ConvertSgr(string match, Func<int, bool, string> mapColor)
    {
        var seq = match[2..^1];
        if (seq.Length == 0)
        {
            return match;
        }

        var parts = seq.Split(';');
        if (parts.Contains("38") || parts.Contains("48"))
        {
            return match;
        }

        var newParts = new List<string>();
        foreach (var p in parts)
        {
            if (p.Length == 0)
            {
                continue;
            }

            if (!int.TryParse(p, out var code))
            {
                newParts.Add(NormalizeDigits(p));
                continue;
            }

            if (!AnsiConstants.MapIndex(code, out var idx))
            {
                newParts.Add(code.ToString());
                continue;
            }

            var fg = (code is >= 30 and <= 37) || (code is >= 90 and <= 97);
            newParts.Add(mapColor(idx, fg));
        }

        if (newParts.Count == 0)
        {
            return match;
        }

        return "\x1b[" + string.Join(";", newParts) + "m";
    }

    public static string UpgradeTo256(string text, int[]? palette = null)
    {
        var pal = palette ?? AnsiConstants.DefaultPalette;
        text = TokenRe().Replace(text, m =>
        {
            var kind = m.Groups[1].Value[0];
            var raw = int.Parse(m.Groups[2].Value);
            var color = pal[raw % 16];
            var prefix = kind == 'P' ? "F" : "B";
            return "{" + prefix + color + "}";
        });
        return SgrRe().Replace(text, m => ConvertSgr(m.Value, (idx, fg) =>
        {
            var color = pal[idx].ToString();
            return fg ? "38;5;" + color : "48;5;" + color;
        }));
    }

    public static string UpgradeToTruecolor(string text, int[]? palette = null)
    {
        var pal = palette ?? AnsiConstants.DefaultPalette;
        var rgbPalette = AnsiConstants.PaletteToRgb(pal);
        text = TokenRe().Replace(text, m =>
        {
            var kind = m.Groups[1].Value[0];
            var raw = int.Parse(m.Groups[2].Value);
            var c = rgbPalette[raw % 16];
            var rgb = $"{c.R};{c.G};{c.B}";
            return kind == 'P' ? $"\x1b[38;2;{rgb}m" : $"\x1b[48;2;{rgb}m";
        });
        return SgrRe().Replace(text, m => ConvertSgr(m.Value, (idx, fg) =>
        {
            var c = rgbPalette[idx];
            var rgb = $"{c.R};{c.G};{c.B}";
            return fg ? "38;2;" + rgb : "48;2;" + rgb;
        }));
    }
}
