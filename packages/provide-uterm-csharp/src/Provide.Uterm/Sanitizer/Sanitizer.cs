//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Sanitizer;

/// <summary>
/// Keystroke unescaping and sanitization shared by direct sessions and MCP.
/// Port of provide.uterm.sanitizer / packages/provide-uterm-go/sanitizer.
/// </summary>
public static class KeystrokeSanitizer
{
    public const int DefaultMaxBytes = 4096;

    private static readonly Dictionary<string, string> SimpleEscapes = new()
    {
        ["n"] = "\n",
        ["r"] = "\r",
        ["t"] = "\t",
        ["e"] = "\x1b",
        ["0"] = "\x00",
        ["\\"] = "\\",
        ["'"] = "'",
        ["\""] = "\"",
    };

    // (?s) mirrors Python re.DOTALL so `\<newline>` is matched (and preserved).
    private static readonly Regex EscapePattern = new(
        @"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))",
        RegexOptions.Singleline | RegexOptions.Compiled);

    /// <summary>Translate terminal-relevant escape sequences in <paramref name="raw"/>.</summary>
    public static string UnescapeKeys(string raw) =>
        EscapePattern.Replace(raw, m =>
        {
            var hex2 = m.Groups[1].Value;
            var hex4 = m.Groups[2].Value;
            var ch = m.Groups[3].Value;
            if (hex2.Length > 0)
            {
                var v = uint.Parse(hex2, NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                return ((char)v).ToString();
            }

            if (hex4.Length > 0)
            {
                var v = uint.Parse(hex4, NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                return char.ConvertFromUtf32((int)v);
            }

            return SimpleEscapes.TryGetValue(ch, out var repl) ? repl : m.Value;
        });

    private static bool AllowedRune(char r)
    {
        if (r is >= (char)0x20 and <= (char)0x7E)
        {
            return true;
        }

        return r is '\t' or '\n' or '\r' or '\v' or '\f' or '\x03' or '\x1b';
    }

    /// <summary>
    /// Filter non-printable bytes while preserving terminal input controls, then
    /// truncate to <paramref name="maxBytes"/>.
    /// </summary>
    public static string SanitizeKeystrokes(string keys, int maxBytes = DefaultMaxBytes)
    {
        var sb = new StringBuilder(keys.Length);
        foreach (var r in keys)
        {
            if (AllowedRune(r))
            {
                sb.Append(r);
            }
        }

        var filtered = sb.ToString();
        return filtered.Length <= maxBytes ? filtered : filtered[..maxBytes];
    }

    /// <summary>Unescape then sanitize keystrokes.</summary>
    public static string PrepareKeystrokes(string raw, int maxBytes = DefaultMaxBytes) =>
        SanitizeKeystrokes(UnescapeKeys(raw), maxBytes);
}
