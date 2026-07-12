//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Screen;

/// <summary>ANSI stripping and bare-SGR cleanup.</summary>
public static partial class ScreenNormalize
{
    [GeneratedRegex("\x1b(?:\\[[0-?]*[ -/]*[@-~]|[@-_])")]
    private static partial Regex AnsiEscapeRe();

    [GeneratedRegex(@"<([^<>\r\n]{1,80})>")]
    private static partial Regex ActionTagRe();

    private static readonly string ScreenPadding = new(' ', 80);

    public const int DefaultMaxActionTags = 8;
    public const int DefaultMaxScreenLines = 30;

    public static string NormalizeTerminalText(string text)
    {
        if (text.Length == 0)
        {
            return "";
        }

        var cleaned = text.Replace("\r\n", "\n").Replace('\r', '\n');
        cleaned = AnsiEscapeRe().Replace(cleaned, "");
        cleaned = StripBareSgrLinePrefix(cleaned);
        cleaned = StripBareSgr(cleaned);
        return cleaned;
    }

    public static string StripAnsi(string text) => NormalizeTerminalText(text);

    private static bool ParseBareSgrBody(IReadOnlyList<int> rs, int i, out int mIndex)
    {
        mIndex = 0;
        var groupLen = 0;
        for (var j = i; j < rs.Count; j++)
        {
            var r = rs[j];
            if (r == ';')
            {
                if (groupLen == 0)
                {
                    return false;
                }

                groupLen = 0;
            }
            else if (PyText.IsPyDigit(r))
            {
                groupLen++;
                if (groupLen > 3)
                {
                    return false;
                }
            }
            else if (r == 'm' && groupLen > 0)
            {
                mIndex = j;
                return true;
            }
            else
            {
                return false;
            }
        }

        return false;
    }

    private static string StripBareSgrLinePrefix(string s)
    {
        var rs = s.EnumerateRunes().Select(r => r.Value).ToList();
        var b = new StringBuilder(s.Length);
        for (var i = 0; i < rs.Count;)
        {
            if (i == 0 || rs[i - 1] == '\n')
            {
                if (ParseBareSgrBody(rs, i, out var m) && m + 1 < rs.Count &&
                    (rs[m + 1] == '<' || (rs[m + 1] >= 'A' && rs[m + 1] <= 'Z')))
                {
                    i = m + 1;
                    continue;
                }
            }

            b.Append(char.ConvertFromUtf32(rs[i]));
            i++;
        }

        return b.ToString();
    }

    private static string StripBareSgr(string s)
    {
        var rs = s.EnumerateRunes().Select(r => r.Value).ToList();
        var b = new StringBuilder(s.Length);
        for (var i = 0; i < rs.Count;)
        {
            if (i == 0 || PyText.IsPySpace(rs[i - 1]))
            {
                if (ParseBareSgrBody(rs, i, out var m) &&
                    (m + 1 >= rs.Count || rs[m + 1] == 0x1b || PyText.IsPySpace(rs[m + 1])))
                {
                    i = m + 1;
                    continue;
                }
            }

            b.Append(char.ConvertFromUtf32(rs[i]));
            i++;
        }

        return b.ToString();
    }

    public static IReadOnlyList<string> ExtractActionTags(string text, int maxTags = DefaultMaxActionTags)
    {
        var output = new List<string>();
        if (text.Length == 0)
        {
            return output;
        }

        if (maxTags < 1)
        {
            maxTags = 1;
        }

        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (Match m in ActionTagRe().Matches(text))
        {
            var tag = PyText.PyStrip(m.Groups[1].Value);
            if (tag.Length == 0)
            {
                continue;
            }

            var key = tag.ToLowerInvariant();
            if (!seen.Add(key))
            {
                continue;
            }

            output.Add(tag);
            if (output.Count >= maxTags)
            {
                break;
            }
        }

        return output;
    }

    public static IReadOnlyList<string> CleanScreenForDisplay(string screen, int maxLines = DefaultMaxScreenLines)
    {
        var lines = new List<string>();
        foreach (var line in screen.Split('\n'))
        {
            if (PyText.PyStrip(line).Length != 0 || !line.StartsWith(ScreenPadding, StringComparison.Ordinal))
            {
                lines.Add(line);
                if (lines.Count >= maxLines)
                {
                    break;
                }
            }
        }

        return lines;
    }
}
