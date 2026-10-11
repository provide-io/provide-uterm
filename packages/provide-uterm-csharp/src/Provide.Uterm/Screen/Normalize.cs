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
    // The escape grammar StripAnsi removes, in named parts so the chunk-carry
    // check (SplitIncompleteEscape) is built from the SAME pieces and cannot
    // drift. Reference: screen.py's _ESC / _CSI_* / _FE_FINAL_BYTE.
    //   CSI: ESC "[" <parameter bytes 0x30-0x3F>* <intermediate bytes 0x20-0x2F>* <final 0x40-0x7E>
    //   Fe:  ESC <one byte 0x40-0x5F> ("[" is one, so a bare "ESC [" is
    //        ambiguous until more arrives)
    internal const string Esc = "\u001b";
    internal const string CsiIntroducer = @"\[";
    internal const string CsiParameterBytes = "[0-?]*";
    internal const string CsiIntermediateBytes = "[ -/]*";
    internal const string CsiFinalByte = "[@-~]";
    internal const string FeFinalByte = "[@-_]";
    internal const string CsiPrefix = CsiIntroducer + CsiParameterBytes + CsiIntermediateBytes;

    /// <summary>Everything StripAnsi removes. Reference: <c>_ANSI_ESCAPE_RE</c>.</summary>
    internal const string AnsiEscapePattern = Esc + "(?:" + CsiPrefix + CsiFinalByte + "|" + FeFinalByte + ")";

    /// <summary>
    /// A proper prefix of something <see cref="AnsiEscapePattern"/> strips: a
    /// bare ESC, or a CSI whose parameter/intermediate bytes have not reached a
    /// final byte. Anchored, as the reference's <c>fullmatch</c>. Reference:
    /// <c>_INCOMPLETE_ESCAPE_RE</c>.
    /// </summary>
    internal const string IncompleteEscapePattern = "^" + Esc + "(?:" + CsiPrefix + @")?\z";

    /// <summary>
    /// Longest tail <see cref="SplitIncompleteEscape"/> holds back, ESC
    /// included. A real sequence is a handful of characters; past this it is
    /// not one (or is hostile), and it is released into the text rather than
    /// carried forever. Reference: <c>MAX_ESCAPE_CARRY</c>.
    /// </summary>
    public const int MaxEscapeCarry = 64;

    [GeneratedRegex(AnsiEscapePattern)]
    private static partial Regex AnsiEscapeRe();

    [GeneratedRegex(IncompleteEscapePattern)]
    private static partial Regex IncompleteEscapeRe();

    /// <summary>
    /// Split <paramref name="text"/> into (complete, carry): carry is an
    /// unterminated trailing escape, for text that arrives in chunks. The
    /// caller strips <c>Complete</c> now and prepends <c>Carry</c> to the next
    /// chunk, so a sequence cut at a boundary (<c>\x1b[1</c> | <c>msudo</c>) is
    /// stripped whole once it completes. Carry is non-empty exactly when the
    /// text from the LAST ESC is a proper prefix of a sequence StripAnsi
    /// removes and is at most <paramref name="maxCarry"/> long (no byte of an
    /// incomplete sequence is ESC, so only the last can start one). Lengths are
    /// UTF-16 units, the reference's code points: a tail the grammar accepts is
    /// all ASCII. Reference: <c>split_incomplete_escape</c>.
    /// </summary>
    public static (string Complete, string Carry) SplitIncompleteEscape(string text, int maxCarry = MaxEscapeCarry)
    {
        var start = text.LastIndexOf('\u001b');
        if (start < 0 || text.Length - start > maxCarry) return (text, "");
        if (!IncompleteEscapeRe().IsMatch(text.AsSpan(start))) return (text, "");
        return (text[..start], text[start..]);
    }

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
