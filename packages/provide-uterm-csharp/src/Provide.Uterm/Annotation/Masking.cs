//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Annotation;

/// <summary>
/// Masks credential-shaped text before a match reaches a description. Port of
/// the Python module <c>provide.uterm.annotation._masking</c>; the pattern
/// strings are identical and <c>testdata/annotation_golden.json</c> pins them.
///
/// THE RULE: a template that names <c>{match}</c> receives
/// <c>Mask(fullMatch)</c> cut to 80 characters -- masked FIRST, so the cut
/// cannot leave the head of a credential behind as a fragment no redaction rule
/// recognises. The masks run in order, each over the output of the one before.
/// Every mask has three groups -- kept prefix, secret, kept suffix -- and every
/// non-overlapping match, left to right, becomes prefix + <see cref="Marker"/> +
/// suffix. The patterns use ASCII-only classes, no <c>\s</c> or IgnoreCase, and
/// no lookaround, so CPython, RE2, ECMAScript and .NET agree on every input.
/// Detection and the match key are unaffected.
/// </summary>
public static class SecretMasking
{
    /// <summary>What every credential-shaped substring of a match becomes.</summary>
    public const string Marker = "[SECRET]";

    /// <summary>The masks, in the order they are applied, as the reference writes them.</summary>
    public static readonly IReadOnlyList<string> Patterns =
    [
        @"([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn][ \t]*:[ \t]*)([^'""\r\n]+)()",
        @"((?:[Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[ \t\n\r\f\v]+)([^ \t\n\r\f\v'""]+)()",
        @"((?:[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]"
            + @"|[Tt][Oo][Kk][Ee][Nn]|[Aa][Pp][Ii][_-]?[Kk][Ee][Yy])['""]?[ \t]*[=:][ \t]*['""]?)"
            + @"([^ \t\n\r\f\v'""]+)()",
        @"((?:^|[ \t])(?:-[uU][ \t]*|--(?:user|proxy-user|password|http-password|ftp-password|proxy-password)"
            + @"(?:[ \t]+|=))['""]?)([^ \t\n\r\f\v'""]+)()",
        @"(://)([^/ \t\n\r\f\v@:'""]*:[^/ \t\n\r\f\v@'""]*)(@)",
        @"()(AKIA[0-9A-Z]{12,})()",
        @"()(gh[psourx]_[A-Za-z0-9_]{8,})()",
        @"()(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)()",
        @"(-----BEGIN[ \t]+[A-Z ]*PRIVATE KEY-----)([^-]+)()",
    ];

    private static readonly Regex[] Masks =
        [.. Patterns.Select(p => new Regex(p, RegexOptions.Compiled | RegexOptions.CultureInvariant))];

    /// <summary><paramref name="text"/> with every credential-shaped substring replaced by <see cref="Marker"/>.</summary>
    public static string Mask(string text)
    {
        foreach (var mask in Masks)
        {
            text = mask.Replace(text, m => m.Groups[1].Value + Marker + m.Groups[3].Value);
        }

        return text;
    }
}
