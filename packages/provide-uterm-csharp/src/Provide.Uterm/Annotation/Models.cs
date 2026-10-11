//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Annotation;

public sealed class AnnotationSpan
{
    public int FromSeq { get; set; }
    public int ToSeq { get; set; }
}

public sealed class Annotation
{
    public string Label { get; set; } = "";
    public string Description { get; set; } = "";
    public string Severity { get; set; } = "";
    public string Source { get; set; } = "";
    public string Principal { get; set; } = "";
    public AnnotationSpan? Span { get; set; }

    /// <summary>
    /// <see cref="ComputeMatchKey"/> of the rule and its full match for a
    /// detector annotation, "" for any other. An in-process identity for
    /// dedupe, not recorded data: <see cref="ToDict"/> omits it and it is never
    /// serialised. Reference: <c>Annotation.match_key</c>.
    /// </summary>
    [JsonIgnore]
    public string MatchKey { get; set; } = "";

    /// <summary>
    /// Identity of one detector match: lowercase hex SHA-256 of the UTF-8 bytes
    /// of <paramref name="ruleId"/>, one NUL byte, then the UTF-8 bytes of
    /// <paramref name="match"/> — the FULL match, not the description's cut.
    /// It names the rule and the exact text matched without keeping the text
    /// (often a secret). Reference: <c>annotation_match_key</c>, which encodes
    /// with <c>surrogatepass</c>: a lone surrogate is encoded as its own
    /// three-byte sequence rather than replaced, so it hashes the same here.
    /// </summary>
    public static string ComputeMatchKey(string ruleId, string match)
    {
        var bytes = new List<byte>((ruleId.Length + match.Length + 1) * 3);
        AppendSurrogatePassUtf8(bytes, ruleId);
        bytes.Add(0);
        AppendSurrogatePassUtf8(bytes, match);
        return Convert.ToHexStringLower(SHA256.HashData(bytes.ToArray()));
    }

    /// <summary>Python's <c>str.encode("utf-8", "surrogatepass")</c> of a UTF-16 string.</summary>
    private static void AppendSurrogatePassUtf8(List<byte> bytes, string text)
    {
        for (var i = 0; i < text.Length; i++)
        {
            int cp = text[i];
            if (char.IsHighSurrogate(text[i]) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1]))
            {
                cp = char.ConvertToUtf32(text[i], text[i + 1]);
                i++;
            }

            if (cp < 0x80)
            {
                bytes.Add((byte)cp);
            }
            else if (cp < 0x800)
            {
                bytes.Add((byte)(0xC0 | (cp >> 6)));
                bytes.Add((byte)(0x80 | (cp & 0x3F)));
            }
            else if (cp < 0x10000)
            {
                bytes.Add((byte)(0xE0 | (cp >> 12)));
                bytes.Add((byte)(0x80 | ((cp >> 6) & 0x3F)));
                bytes.Add((byte)(0x80 | (cp & 0x3F)));
            }
            else
            {
                bytes.Add((byte)(0xF0 | (cp >> 18)));
                bytes.Add((byte)(0x80 | ((cp >> 12) & 0x3F)));
                bytes.Add((byte)(0x80 | ((cp >> 6) & 0x3F)));
                bytes.Add((byte)(0x80 | (cp & 0x3F)));
            }
        }
    }

    public Dictionary<string, object?> ToDict()
    {
        var result = new Dictionary<string, object?>
        {
            ["label"] = Label,
            ["description"] = Description,
            ["severity"] = Severity,
            ["source"] = Source,
            ["principal"] = Principal,
            ["span"] = null,
        };
        if (Span is not null)
        {
            result["span"] = new Dictionary<string, object?>
            {
                ["from_seq"] = Span.FromSeq,
                ["to_seq"] = Span.ToSeq,
            };
        }

        return result;
    }
}

public sealed class DetectionRule
{
    public string RuleId { get; set; } = "";
    public string Label { get; set; } = "";
    public Regex? Pattern { get; set; }
    public string Severity { get; set; } = "info";
    public string DescriptionTemplate { get; set; } = "";
    public HashSet<string> EventTypes { get; set; } = new(StringComparer.Ordinal);
    public string Category { get; set; } = "";

    public bool AppliesTo(string eventType) => EventTypes.Contains(eventType);
}
