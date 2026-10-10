//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;
using Provide.Uterm.Redaction;

namespace Provide.Uterm.Tests;

/// <summary>
/// <see cref="StreamRedactor"/> and <see cref="RedactionDefaults"/> against the
/// differential corpus generated from the Python reference's
/// <c>StreamRedactor(default_rules())</c>
/// (<c>packages/provide-uterm-ts/testdata/gen_stream_redaction_golden.py</c>,
/// run with its output pointed at this port's testdata; byte-identical to the
/// TypeScript port's copy).
/// </summary>
public sealed class StreamRedactionGoldenTests
{
    private static readonly JsonNode Golden =
        JsonNode.Parse(File.ReadAllText(TestData.PathTo("stream_redaction_golden.json")))!;

    private static List<RedactionRule> Rules(string key) =>
        Golden[key]!.AsArray()
            .Select(r => new RedactionRule((string)r!["pattern"]!, (string)r["replacement"]!))
            .ToList();

    private static void AssertCases(StreamRedactor redactor, string key)
    {
        var cases = Golden[key]!.AsArray();
        Assert.NotEmpty(cases);
        foreach (var c in cases)
        {
            Assert.Equal((string)c!["output"]!, redactor.Redact((string)c["input"]!));
        }
    }

    [Fact]
    public void TheDefaultRulesAreTheReferencesVerbatim()
    {
        Assert.Equal(Rules("default_rules"), RedactionDefaults.DefaultRules());
    }

    [Theory]
    [InlineData("default", "default_rules")]
    [InlineData("custom", "custom_rules")]
    [InlineData("uniform", "uniform_rules")]
    public void RedactsAsTheReferenceDoes(string cases, string rules)
    {
        AssertCases(new StreamRedactor(Rules(rules)), cases);
    }

    [Fact]
    public void TheDefaultSetRedactsAsTheReferenceDoes()
    {
        AssertCases(new StreamRedactor(RedactionDefaults.DefaultRules()), "default");
    }

    [Fact]
    public void NoRulesIsTheIdentity()
    {
        AssertCases(new StreamRedactor([]), "empty");
        AssertCases(new StreamRedactor(null), "empty");
    }

    [Fact]
    public void ARuleThatDoesNotCompileIsSkipped()
    {
        var redactor = new StreamRedactor([new RedactionRule("(unclosed", "<BAD>"), new RedactionRule("b+", "<B>")]);

        Assert.Equal("a<B>c(unclosed", redactor.Redact("abbc(unclosed"));
    }

    [Fact]
    public void TheDefaultReplacementIsRedacted()
    {
        Assert.Equal("[REDACTED]", new RedactionRule("x").Replacement);
    }
}
