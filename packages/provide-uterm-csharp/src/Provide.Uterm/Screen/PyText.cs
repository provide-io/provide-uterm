//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Screen;

internal static class PyText
{
    internal static bool IsPySpace(int r) =>
        char.IsWhiteSpace((char)r) && r <= 0xFFFF
            ? RuneWhiteSpace(r)
            : RuneWhiteSpace(r);

    private static bool RuneWhiteSpace(int r)
    {
        if (r >= 0 && r <= char.MaxValue && char.IsWhiteSpace((char)r))
        {
            return true;
        }

        // Unicode White_Space property for non-BMP is rare; also include 0x1c-0x1f
        if (r >= 0x1c && r <= 0x1f)
        {
            return true;
        }

        // Use Rune.IsWhiteSpace for general case
        try
        {
            return Rune.IsWhiteSpace(new Rune(r));
        }
        catch
        {
            return false;
        }
    }

    internal static bool IsPyDigit(int r) =>
        r <= char.MaxValue ? char.IsDigit((char)r) : Rune.IsDigit(new Rune(r));

    internal static string PyStrip(string s)
    {
        var runes = s.EnumerateRunes().ToList();
        var start = 0;
        var end = runes.Count;
        while (start < end && IsPySpace(runes[start].Value))
        {
            start++;
        }

        while (end > start && IsPySpace(runes[end - 1].Value))
        {
            end--;
        }

        if (start == 0 && end == runes.Count)
        {
            return s;
        }

        return string.Concat(runes.Skip(start).Take(end - start).Select(r => r.ToString()));
    }

    private static bool IsPyLineTerminator(int r) =>
        r is '\n' or '\r' or '\v' or '\f' or 0x1c or 0x1d or 0x1e or 0x85 or 0x2028 or 0x2029;

    internal static List<string> PySplitLines(string s)
    {
        var rs = s.EnumerateRunes().Select(r => r.Value).ToList();
        var lines = new List<string>();
        var start = 0;
        for (var i = 0; i < rs.Count;)
        {
            if (!IsPyLineTerminator(rs[i]))
            {
                i++;
                continue;
            }

            lines.Add(string.Concat(rs.Skip(start).Take(i - start).Select(r => char.ConvertFromUtf32(r))));
            if (rs[i] == '\r' && i + 1 < rs.Count && rs[i + 1] == '\n')
            {
                i++;
            }

            i++;
            start = i;
        }

        if (start < rs.Count)
        {
            lines.Add(string.Concat(rs.Skip(start).Select(r => char.ConvertFromUtf32(r))));
        }

        return lines;
    }
}
