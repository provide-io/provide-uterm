//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections;

namespace Provide.Uterm.Redaction;

/// <summary>
/// Structure-aware redaction for recorder payloads.
/// Port of provide.uterm.redaction.redact_structure / Go redaction.RedactStructure.
/// </summary>
public static class RedactionStructure
{
    /// <summary>
    /// Container levels walked. Real frames are two or three levels deep; the cap keeps a
    /// hostile frame nested thousands deep from overflowing the stack of every recorder that
    /// redacts it.
    /// </summary>
    public const int RedactMaxDepth = 32;

    /// <summary>
    /// Replaces a container at or past the depth cap. The subtree is replaced whole, never
    /// returned unwalked: an unwalked subtree is an unredacted one.
    /// </summary>
    public const string DepthLimitMarker = "[NESTED_REDACTED]";

    /// <summary>Replaces the value of a sensitive key under key-aware redaction.</summary>
    public const string SensitiveKeyMarker = "[SECRET_REDACTED]";

    /// <summary>Field names whose value is a credential regardless of what it looks like.</summary>
    public static readonly IReadOnlySet<string> SensitiveKeyNames = new HashSet<string>(StringComparer.Ordinal)
    {
        "token", "password", "passwd", "passphrase", "secret",
        "api_key", "apikey", "authorization", "cookie", "private_key",
    };

    /// <summary>Any key ending in one of these is sensitive too (resume_token, client_secret, ...).</summary>
    public static readonly IReadOnlyList<string> SensitiveKeySuffixes =
        ["_token", "_password", "_passphrase", "_secret", "_api_key"];

    /// <summary>Whether <paramref name="key"/> names a credential field (lower-cased, '-' folded to '_').</summary>
    public static bool IsSensitiveKey(string key)
    {
        var folded = key.ToLowerInvariant().Replace('-', '_');
        if (SensitiveKeyNames.Contains(folded))
        {
            return true;
        }

        foreach (var suffix in SensitiveKeySuffixes)
        {
            if (folded.EndsWith(suffix, StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>
    /// Return a redacted copy of a JSON-like value: strings go through <paramref name="redactor"/>
    /// at any depth; dictionaries and sequences are rebuilt as
    /// <c>Dictionary&lt;string, object?&gt;</c> / <c>List&lt;object?&gt;</c> (keys kept, input never
    /// mutated; <c>byte[]</c> is a scalar, as System.Text.Json treats it); a container
    /// <paramref name="maxDepth"/> levels below the root becomes <see cref="DepthLimitMarker"/>;
    /// with <paramref name="keyAware"/>, a non-null value under a sensitive key becomes
    /// <see cref="SensitiveKeyMarker"/>; every other value is returned unchanged.
    /// </summary>
    public static object? Redact(object? value, Redactor redactor, bool keyAware = false, int maxDepth = RedactMaxDepth) =>
        Walk(value, redactor, keyAware, maxDepth, 0);

    private static object? Walk(object? value, Redactor redactor, bool keyAware, int maxDepth, int depth)
    {
        switch (value)
        {
            case string s:
                return redactor(s);
            case null:
            case byte[]:
                return value;
            case IDictionary dict:
                if (depth >= maxDepth)
                {
                    return DepthLimitMarker;
                }

                var map = new Dictionary<string, object?>(dict.Count, StringComparer.Ordinal);
                foreach (DictionaryEntry entry in dict)
                {
                    var key = Convert.ToString(entry.Key, System.Globalization.CultureInfo.InvariantCulture) ?? "";
                    map[key] = keyAware && entry.Value is not null && IsSensitiveKey(key)
                        ? SensitiveKeyMarker
                        : Walk(entry.Value, redactor, keyAware, maxDepth, depth + 1);
                }

                return map;
            case IEnumerable sequence:
                if (depth >= maxDepth)
                {
                    return DepthLimitMarker;
                }

                var list = new List<object?>();
                foreach (var item in sequence)
                {
                    list.Add(Walk(item, redactor, keyAware, maxDepth, depth + 1));
                }

                return list;
            default:
                return value;
        }
    }
}
