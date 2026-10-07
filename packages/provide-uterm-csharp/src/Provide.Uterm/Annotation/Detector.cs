//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Annotation;

/// <summary>
/// Hot-path scanner that matches terminal event text against detection rules.
/// Port of the Python module <c>provide.uterm.annotation._detector</c>, pinned
/// by <c>testdata/annotation_golden.json</c>.
///
/// Getting this wrong is quiet in both directions: a missed match means an
/// incident review never sees the moment, and a leaked one means the secret
/// itself ends up in the annotation, which flows to recordings and logs.
/// </summary>
public sealed partial class PatternDetector
{
    /// <summary>How much of a match reaches the description.</summary>
    public const int DescriptionTruncate = 80;

    /// <summary>Stands in for the match when a template could not be formatted.</summary>
    private const string FallbackPlaceholder = "<unavailable>";

    private readonly IReadOnlyList<DetectionRule> _rules;

    /// <summary>Scan with <paramref name="rules"/>, or the built-in set when null.</summary>
    public PatternDetector(IEnumerable<DetectionRule>? rules = null)
    {
        _rules = rules?.ToList() ?? BuiltinDetectionRules.All;
    }

    /// <summary>A copy of the built-in rules, in the order they are tried.</summary>
    public static List<DetectionRule> BuiltinRules() => [.. BuiltinDetectionRules.All];

    /// <summary>Everything <paramref name="text"/> matches, at most one annotation per category.</summary>
    public IReadOnlyList<Annotation> Detect(string eventType, string text, int seq = 0) =>
        Scan(eventType, text, seq).Annotations;

    /// <summary>
    /// Like <see cref="Detect"/>, but also the end offset of the furthest match
    /// (zero when nothing matched). <see cref="StreamingDetector"/> uses it to
    /// carry only the text after a completed match, so the match is not
    /// reported twice while a second secret starting right after it still
    /// bridges the next boundary.
    /// </summary>
    public (IReadOnlyList<Annotation> Annotations, int MatchEnd) Scan(string eventType, string text, int seq = 0)
    {
        if (string.IsNullOrEmpty(text))
        {
            return ([], 0);
        }

        var results = new List<Annotation>();
        var seenCategories = new HashSet<string>(StringComparer.Ordinal);
        var matchEnd = 0;
        foreach (var rule in _rules)
        {
            // Only the first rule to match a category counts: otherwise one line
            // mentioning a password produces several near-identical annotations.
            if (seenCategories.Contains(rule.Category) || !rule.AppliesTo(eventType) || rule.Pattern is null)
            {
                continue;
            }

            var match = rule.Pattern.Match(text);
            if (!match.Success)
            {
                continue;
            }

            seenCategories.Add(rule.Category);
            matchEnd = Math.Max(matchEnd, match.Index + match.Length);
            var matchText = match.Value.Length > DescriptionTruncate ? match.Value[..DescriptionTruncate] : match.Value;
            // A malformed template must not leak the raw match -- the secret --
            // into a description that flows to recordings and logs.
            var description = FormatDescription(rule.DescriptionTemplate, matchText, eventType)
                              ?? rule.Label + ": " + FallbackPlaceholder;
            results.Add(new Annotation
            {
                Label = rule.Label,
                Description = description,
                Severity = rule.Severity,
                Source = "detector",
                Principal = "system",
                Span = new AnnotationSpan { FromSeq = seq, ToSeq = seq },
            });
        }

        return (results, matchEnd);
    }

    [GeneratedRegex(@"\{([^{}]*)\}")]
    private static partial Regex TemplateField();

    /// <summary>
    /// Python's <c>str.format(match=..., event_type=...)</c> for the two fields
    /// a rule may name; null for any other field, where Python raises.
    /// </summary>
    private static string? FormatDescription(string template, string match, string eventType)
    {
        var failed = false;
        var description = TemplateField().Replace(template, field =>
        {
            switch (field.Groups[1].Value)
            {
                case "match":
                    return match;
                case "event_type":
                    return eventType;
                default:
                    failed = true;
                    return "";
            }
        });
        return failed ? null : description;
    }
}

/// <summary>
/// Catches a pattern split across consecutive chunks. Port of the Python module
/// <c>provide.uterm.annotation._streaming</c>.
///
/// Stateful: use one per logical stream (one per session and direction). The
/// wrapped <see cref="PatternDetector"/> stays stateless and may be shared.
/// </summary>
public sealed class StreamingDetector
{
    /// <summary>The longest fixed-shape secret expected to bridge a boundary.</summary>
    public const int DefaultMaxCarry = 512;

    private readonly PatternDetector _inner;
    private readonly int _maxCarry;
    private string _carry = "";

    public StreamingDetector(PatternDetector? inner = null, int maxCarry = DefaultMaxCarry)
    {
        _inner = inner ?? new PatternDetector();
        _maxCarry = maxCarry;
    }

    /// <summary>
    /// Scan <paramref name="text"/> joined to the carried tail. A match belongs
    /// to the chunk it completes in; the tail kept is the window after the
    /// furthest match, bounded to the last <c>maxCarry</c> characters.
    /// </summary>
    public IReadOnlyList<Annotation> Detect(string eventType, string text, int seq = 0)
    {
        if (string.IsNullOrEmpty(text))
        {
            return [];
        }

        var window = _carry + text;
        var (annotations, matchEnd) = _inner.Scan(eventType, window, seq);
        var tail = window[matchEnd..];
        _carry = tail.Length > _maxCarry ? tail[^_maxCarry..] : tail;
        return annotations;
    }

    /// <summary>Forget the carried tail (screen clear / resync).</summary>
    public void Reset() => _carry = "";
}
