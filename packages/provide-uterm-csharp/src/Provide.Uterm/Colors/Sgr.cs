//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Colors;

/// <summary>SGR parameter-list rewriting for color downgrade.</summary>
public static partial class Sgr
{
    public static readonly Regex SgrRegexp = SgrPattern();

    [GeneratedRegex(@"\x1b\[([0-9;]*)m")]
    private static partial Regex SgrPattern();

    private static readonly int[] Fg16 = [30, 34, 32, 36, 31, 35, 33, 37, 90, 94, 92, 96, 91, 95, 93, 97];
    private static readonly int[] Bg16 = [40, 44, 42, 46, 41, 45, 43, 47, 100, 104, 102, 106, 101, 105, 103, 107];

    private static bool IsDigits(string s)
    {
        if (s.Length == 0)
        {
            return false;
        }

        foreach (var c in s)
        {
            if (c is < '0' or > '9')
            {
                return false;
            }
        }

        return true;
    }

    private static int ParseComponent(string s)
    {
        var trimmed = s.TrimStart('0');
        if (trimmed.Length > 9)
        {
            return 1 << 30;
        }

        return int.TryParse(s, out var v) ? v : 0;
    }

    public static string RewriteParams(string parameters, ColorMode mode)
    {
        if (parameters.Length == 0)
        {
            return "\x1b[" + parameters + "m";
        }

        var parts = parameters.Split(';');
        var output = new List<string>(parts.Length);
        var i = 0;
        var n = parts.Length;
        while (i < n)
        {
            if (i + 4 < n &&
                (parts[i] == "38" || parts[i] == "48") &&
                parts[i + 1] == "2" &&
                IsDigits(parts[i + 2]) && IsDigits(parts[i + 3]) && IsDigits(parts[i + 4]))
            {
                var r = ParseComponent(parts[i + 2]);
                var g = ParseComponent(parts[i + 3]);
                var b = ParseComponent(parts[i + 4]);
                var isFg = parts[i] == "38";
                if (mode == ColorMode.Mode256)
                {
                    var code = Rgb.RgbTo256(r, g, b);
                    output.Add(isFg ? "38" : "48");
                    output.Add("5");
                    output.Add(code.ToString());
                }
                else
                {
                    var idx = Rgb.RgbTo16Index(r, g, b);
                    output.Add((isFg ? Fg16[idx] : Bg16[idx]).ToString());
                }

                i += 5;
                continue;
            }

            output.Add(parts[i]);
            i++;
        }

        return "\x1b[" + string.Join(";", output) + "m";
    }
}
