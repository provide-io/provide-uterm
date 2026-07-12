//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Redaction;

/// <summary>Rewrites text, replacing sensitive spans.</summary>
public delegate string Redactor(string text);

/// <summary>
/// Reusable redaction helpers for terminal logs and captures.
/// Port of provide.uterm.redaction / packages/provide-uterm-go/redaction.
/// </summary>
public static class Redaction
{
    /// <summary>
    /// Build a text redactor from regex patterns. With no patterns the returned
    /// redactor is the identity function.
    /// </summary>
    public static Redactor MakeRedactor(IEnumerable<string> patterns)
    {
        var compiled = new List<Regex>();
        foreach (var pattern in patterns)
        {
            compiled.Add(new Regex(pattern, RegexOptions.Compiled));
        }

        if (compiled.Count == 0)
        {
            return static text => text;
        }

        return text =>
        {
            var result = text;
            foreach (var re in compiled)
            {
                result = re.Replace(result, "[REDACTED]");
            }

            return result;
        };
    }

    /// <summary>Apply redactor to text, preserving identity when no redactor is configured.</summary>
    public static string RedactText(string text, Redactor? redactor) =>
        redactor is null ? text : redactor(text);
}
