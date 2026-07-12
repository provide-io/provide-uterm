//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

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
