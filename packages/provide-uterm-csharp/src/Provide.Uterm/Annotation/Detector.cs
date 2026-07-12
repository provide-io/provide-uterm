//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Annotation;

/// <summary>
/// Hot-path pattern detector for annotation-worthy terminal events.
/// Port of packages/provide-uterm-go/annotation detector surface.
/// </summary>
public sealed class PatternDetector
{
    private readonly List<DetectionRule> _rules;

    public PatternDetector(IEnumerable<DetectionRule>? rules = null)
    {
        _rules = rules?.ToList() ?? BuiltinRules();
    }

    public static List<DetectionRule> BuiltinRules() =>
    [
        new DetectionRule
        {
            RuleId = "credential_exposure",
            Label = "credential_exposure",
            Pattern = new Regex(@"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S+", RegexOptions.Compiled),
            Severity = "high",
            DescriptionTemplate = "Possible credential exposure",
            EventTypes = ["read", "write", "output"],
            Category = "security",
        },
        new DetectionRule
        {
            RuleId = "privilege_escalation",
            Label = "privilege_escalation",
            Pattern = new Regex(@"(?i)\b(sudo|su\s+root|doas)\b", RegexOptions.Compiled),
            Severity = "medium",
            DescriptionTemplate = "Privilege escalation attempt",
            EventTypes = ["write", "input"],
            Category = "security",
        },
        new DetectionRule
        {
            RuleId = "destructive_command",
            Label = "destructive_command",
            Pattern = new Regex(@"(?i)\b(rm\s+-rf|mkfs|dd\s+if=)\b", RegexOptions.Compiled),
            Severity = "high",
            DescriptionTemplate = "Destructive command",
            EventTypes = ["write", "input"],
            Category = "safety",
        },
    ];

    public IReadOnlyList<Annotation> Detect(string eventType, string text, int seq = 0)
    {
        var results = new List<Annotation>();
        foreach (var rule in _rules)
        {
            if (!rule.AppliesTo(eventType) || rule.Pattern is null)
            {
                continue;
            }

            if (!rule.Pattern.IsMatch(text))
            {
                continue;
            }

            results.Add(new Annotation
            {
                Label = rule.Label,
                Description = rule.DescriptionTemplate,
                Severity = rule.Severity,
                Source = rule.RuleId,
                Span = new AnnotationSpan { FromSeq = seq, ToSeq = seq },
            });
        }

        return results;
    }
}

/// <summary>Stateful detector that bridges pattern matches split across chunks.</summary>
public sealed class StreamingDetector
{
    private readonly PatternDetector _inner;
    private string _carry = "";
    private const int MaxCarry = 4096;

    public StreamingDetector(PatternDetector? inner = null) => _inner = inner ?? new PatternDetector();

    public IReadOnlyList<Annotation> Feed(string eventType, string chunk, int seq = 0)
    {
        var combined = _carry + chunk;
        var hits = _inner.Detect(eventType, combined, seq);
        _carry = combined.Length > MaxCarry ? combined[^MaxCarry..] : combined;
        return hits;
    }

    public void Reset() => _carry = "";
}
