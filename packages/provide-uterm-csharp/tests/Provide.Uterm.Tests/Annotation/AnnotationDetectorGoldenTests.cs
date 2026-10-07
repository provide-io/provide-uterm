//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Provide.Uterm.Annotation;

namespace Provide.Uterm.Tests.Annotation;

/// <summary>
/// The detector against the differential corpus generated from the Python
/// reference (<c>packages/provide-uterm-ts/testdata/gen_annotation_golden.py</c>;
/// this file is a byte-identical copy of the TypeScript port's).
///
/// The corpus sat in testdata unread while the detector here was a three-rule
/// stand-in that matched nothing on <c>send</c>, never filled its description
/// templates, and re-reported a completed match on every later chunk. Wiring
/// that into a recording would have produced annotations no other backend
/// does, so the corpus is what pins it now.
/// </summary>
public sealed class AnnotationDetectorGoldenTests
{
    private static readonly JsonNode Golden =
        JsonNode.Parse(File.ReadAllText(TestData.PathTo("annotation_golden.json")))!;

    private static JsonNode Describe(IEnumerable<Provide.Uterm.Annotation.Annotation> annotations) =>
        JsonSerializer.SerializeToNode(annotations.Select(a => a.ToDict()).ToList())!;

    private static void AssertSame(JsonNode? expected, JsonNode actual, string because) =>
        Assert.True(
            JsonNode.DeepEquals(expected, actual),
            $"{because}: expected {expected?.ToJsonString()} got {actual.ToJsonString()}");

    [Fact]
    public void BuiltinRulesMatchTheReferenceInOrder()
    {
        var actual = PatternDetector.BuiltinRules().Select(r => new Dictionary<string, object?>
        {
            ["rule_id"] = r.RuleId,
            ["label"] = r.Label,
            ["pattern"] = r.Pattern?.ToString(),
            ["severity"] = r.Severity,
            ["description_template"] = r.DescriptionTemplate,
            ["event_types"] = r.EventTypes.Order(StringComparer.Ordinal).ToList(),
            ["category"] = r.Category,
        }).ToList();

        AssertSame(Golden["rules"], JsonSerializer.SerializeToNode(actual)!, "rules");
        var categories = PatternDetector.BuiltinRules().Select(r => r.Category).Distinct().ToList();
        AssertSame(Golden["categories_in_order"], JsonSerializer.SerializeToNode(categories)!, "categories");
    }

    [Fact]
    public void EveryScanCaseMatchesTheReference()
    {
        var detector = new PatternDetector();
        foreach (var scan in Golden["scans"]!.AsArray())
        {
            var name = scan!["name"]!.GetValue<string>();
            var eventType = scan["event_type"]!.GetValue<string>();
            var text = scan["text"]!.GetValue<string>();

            var (annotations, matchEnd) = detector.Scan(eventType, text, 7);

            AssertSame(scan["annotations"], Describe(annotations), name);
            Assert.True(scan["match_end"]!.GetValue<int>() == matchEnd, $"{name}: match_end {matchEnd}");
            Assert.True(scan["detect_matches_scan"]!.GetValue<bool>(), name);
            AssertSame(scan["annotations"], Describe(detector.Detect(eventType, text, 7)), name + " (detect)");
        }
    }

    [Fact]
    public void EveryStreamCaseMatchesTheReference()
    {
        foreach (var stream in Golden["streams"]!.AsArray())
        {
            var name = stream!["name"]!.GetValue<string>();
            var streaming = new StreamingDetector(new PatternDetector());
            var index = 0;
            foreach (var step in stream["steps"]!.AsArray())
            {
                var chunk = step!["chunk"]!.GetValue<string>();
                AssertSame(step["annotations"], Describe(streaming.Detect("read", chunk, index)), $"{name} #{index}");
                index++;
            }
        }
    }

    [Fact]
    public void CarryIsBoundedAndForgottenOnReset()
    {
        var carry = Golden["carry"]!;
        Assert.Equal(StreamingDetector.DefaultMaxCarry, carry["default_max_carry"]!.GetValue<int>());

        var bounded = new StreamingDetector(new PatternDetector(), maxCarry: 8);
        bounded.Detect("read", "0123456789abcdef", 0);
        AssertSame(carry["an_empty_chunk_produces_nothing"], Describe(bounded.Detect("read", "", 1)), "empty");

        var resetting = new StreamingDetector(new PatternDetector());
        resetting.Detect("read", "AKIAABC", 0);
        resetting.Reset();
        AssertSame(carry["a_reset_forgets_the_tail"], Describe(resetting.Detect("read", "DEFGHIJKL", 1)), "reset");

        var kept = new StreamingDetector(new PatternDetector());
        kept.Detect("read", "AKIAABC", 0);
        AssertSame(carry["without_a_reset_it_bridges"], Describe(kept.Detect("read", "DEFGHIJKL", 1)), "kept");

        var tight = new StreamingDetector(new PatternDetector(), maxCarry: 4);
        tight.Detect("read", "zzzzAKIAABCDEFGH", 0);
        AssertSame(Golden["bounded_carry"]!["too_small_to_bridge"], Describe(tight.Detect("read", "IJKL", 1)), "tight");

        var roomy = new StreamingDetector(new PatternDetector(), maxCarry: 64);
        roomy.Detect("read", "zzzzAKIAABCDEFGH", 0);
        AssertSame(Golden["bounded_carry"]!["large_enough"], Describe(roomy.Detect("read", "IJKL", 1)), "roomy");
    }

    private static DetectionRule TemplateRule(string pattern, string template) => new()
    {
        RuleId = "t",
        Label = "test",
        Pattern = new Regex(pattern),
        Severity = "low",
        DescriptionTemplate = template,
        EventTypes = new HashSet<string>(StringComparer.Ordinal) { "read" },
        Category = "test",
    };

    private static string DescriptionFor(string pattern, string template, string text) =>
        new PatternDetector([TemplateRule(pattern, template)]).Detect("read", text, 1)[0].Description;

    [Fact]
    public void TemplatesFormatAndNeverLeakTheMatchWhenBroken()
    {
        var templates = Golden["templates"]!;
        Assert.Equal(PatternDetector.DescriptionTruncate, templates["truncate_at"]!.GetValue<int>());
        Assert.Equal(
            templates["a_good_template"]!.GetValue<string>(),
            DescriptionFor("secret-value", "found {match} in {event_type}", "a secret-value here"));
        Assert.Equal(
            templates["a_missing_key"]!.GetValue<string>(),
            DescriptionFor("secret-value", "found {nonexistent}", "a secret-value here"));
        Assert.Equal(
            templates["a_positional_field"]!.GetValue<string>(),
            DescriptionFor("secret-value", "found {0}", "a secret-value here"));
        Assert.Equal(
            templates["a_long_match_is_truncated"]!.GetValue<string>(),
            DescriptionFor("x+", "found {match}", new string('x', 200)));
    }

    [Fact]
    public void AnnotationsSerialiseAsTheReferenceDoes()
    {
        var empty = new Provide.Uterm.Annotation.Annotation
        {
            Label = "l", Description = "d", Severity = "s", Source = "src", Principal = "p",
        };
        AssertSame(Golden["empty_annotation"], JsonSerializer.SerializeToNode(empty.ToDict())!, "empty");

        var spanned = new Provide.Uterm.Annotation.Annotation
        {
            Label = "l", Description = "d", Severity = "s", Source = "src", Principal = "p",
            Span = new AnnotationSpan { FromSeq = 1, ToSeq = 2 },
        };
        AssertSame(Golden["annotation_with_span"], JsonSerializer.SerializeToNode(spanned.ToDict())!, "span");
    }
}
