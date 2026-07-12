//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Screen;

public sealed class MenuOption
{
    public string Key { get; set; } = "";
    public string Description { get; set; } = "";
}

public sealed class NumberedItem
{
    public string Number { get; set; } = "";
    public string Description { get; set; } = "";
}

public static class Extract
{
    public static IReadOnlyList<MenuOption> ExtractMenuOptions(string screen, string pattern = "")
    {
        if (pattern.Length == 0)
        {
            return ExtractMenuOptionsDefault(screen);
        }

        var options = new List<MenuOption>();
        try
        {
            var re = new Regex(pattern);
            foreach (Match m in re.Matches(screen))
            {
                if (m.Groups.Count < 3)
                {
                    continue;
                }

                var description = PyText.PyStrip(m.Groups[2].Value);
                if (description.Length != 0)
                {
                    options.Add(new MenuOption { Key = m.Groups[1].Value, Description = description });
                }
            }
        }
        catch (ArgumentException)
        {
            // invalid pattern
        }

        return options;
    }

    private static List<MenuOption> ExtractMenuOptionsDefault(string screen)
    {
        var options = new List<MenuOption>();
        var rs = screen.EnumerateRunes().Select(r => r.Value).ToList();
        var n = rs.Count;
        for (var i = 0; i < n;)
        {
            if (!IsMenuOpener(rs[i]) || i + 2 >= n || !IsMenuKey(rs[i + 1]) || !IsMenuCloser(rs[i + 2]))
            {
                i++;
                continue;
            }

            var wsStart = i + 3;
            var wsEnd = wsStart;
            while (wsEnd < n && PyText.IsPySpace(rs[wsEnd]))
            {
                wsEnd++;
            }

            var matched = false;
            for (var descStart = wsEnd; descStart > wsStart && !matched; descStart--)
            {
                for (var d = descStart + 1; d <= n; d++)
                {
                    var c = rs[d - 1];
                    if (c is '<' or '[' or '(' or '\n')
                    {
                        break;
                    }

                    if (!MenuLookahead(rs, d))
                    {
                        continue;
                    }

                    var description = PyText.PyStrip(string.Concat(rs.Skip(descStart).Take(d - descStart).Select(r => char.ConvertFromUtf32(r))));
                    if (description.Length != 0)
                    {
                        options.Add(new MenuOption { Key = char.ConvertFromUtf32(rs[i + 1]), Description = description });
                    }

                    i = d;
                    matched = true;
                    break;
                }
            }

            if (!matched)
            {
                i++;
            }
        }

        return options;
    }

    private static bool IsMenuOpener(int r) => r is '<' or '[' or '(';
    private static bool IsMenuCloser(int r) => r is '>' or ']' or ')';
    private static bool IsMenuKey(int r) => (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9');

    private static bool MenuLookahead(IReadOnlyList<int> rs, int p)
    {
        var q = p;
        while (q < rs.Count && PyText.IsPySpace(rs[q]))
        {
            q++;
        }

        if (q < rs.Count && IsMenuOpener(rs[q]))
        {
            return true;
        }

        return p == rs.Count || (p == rs.Count - 1 && rs[p] == '\n');
    }

    public static IReadOnlyList<NumberedItem> ExtractNumberedList(string screen, string pattern = "")
    {
        var items = new List<NumberedItem>();
        if (pattern.Length == 0)
        {
            foreach (var line in PyText.PySplitLines(screen))
            {
                if (!MatchNumberedLine(line, out var number, out var rawDesc))
                {
                    continue;
                }

                var description = PyText.PyStrip(rawDesc);
                if (description.Length != 0)
                {
                    items.Add(new NumberedItem { Number = number, Description = description });
                }
            }

            return items;
        }

        try
        {
            var re = new Regex(pattern);
            foreach (var line in PyText.PySplitLines(screen))
            {
                var m = re.Match(line);
                if (!m.Success || m.Groups.Count < 3)
                {
                    continue;
                }

                var description = PyText.PyStrip(m.Groups[2].Value);
                if (description.Length != 0)
                {
                    items.Add(new NumberedItem { Number = m.Groups[1].Value, Description = description });
                }
            }
        }
        catch (ArgumentException)
        {
        }

        return items;
    }

    private static bool MatchNumberedLine(string line, out string number, out string rawDesc)
    {
        number = "";
        rawDesc = "";
        var rs = line.EnumerateRunes().Select(r => r.Value).ToList();
        var n = rs.Count;
        var i = 0;
        while (i < n && PyText.IsPySpace(rs[i]))
        {
            i++;
        }

        var digStart = i;
        while (i < n && PyText.IsPyDigit(rs[i]))
        {
            i++;
        }

        if (i == digStart || i >= n || (rs[i] != '.' && rs[i] != ')'))
        {
            return false;
        }

        number = string.Concat(rs.Skip(digStart).Take(i - digStart).Select(r => char.ConvertFromUtf32(r)));
        var restStart = i + 1;
        var w = restStart;
        while (w < n && PyText.IsPySpace(rs[w]))
        {
            w++;
        }

        if (w == restStart)
        {
            return false;
        }

        if (w < n)
        {
            rawDesc = string.Concat(rs.Skip(w).Select(r => char.ConvertFromUtf32(r)));
            return true;
        }

        if (w - restStart < 2)
        {
            return false;
        }

        rawDesc = char.ConvertFromUtf32(rs[n - 1]);
        return true;
    }

    public static Dictionary<string, string> ExtractKeyValuePairs(string screen, IReadOnlyDictionary<string, string> patterns)
    {
        var data = new Dictionary<string, string>();
        foreach (var (field, pat) in patterns)
        {
            try
            {
                var re = new Regex("(?i)" + pat);
                var m = re.Match(screen);
                if (m.Success && m.Groups.Count >= 2)
                {
                    data[field] = m.Groups[1].Value;
                }
            }
            catch (ArgumentException)
            {
            }
        }

        return data;
    }
}
