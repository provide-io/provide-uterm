//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Provide.Uterm.Annotation;

namespace Provide.Uterm.Tests.Annotation;

/// <summary>
/// <see cref="Provide.Uterm.Annotation.Annotation.MatchKey"/>: a hashed (rule,
/// matched text) identity, never serialised. Parity with the reference's
/// <c>provide-uterm-annotation/tests/test_annotation_match_key.py</c>, and with
/// <c>annotation_match_key</c> itself through <c>read_path_golden.json</c>.
/// </summary>
public sealed class AnnotationDetectorMatchKeyTests
{
    private const string KeyA = "AKIAIOSFODNN7EXAMPLE"; // pragma: allowlist secret
    private const string KeyB = "AKIAJ2345678ABCDWXYZ"; // pragma: allowlist secret

    private static readonly JsonNode Golden =
        JsonNode.Parse(File.ReadAllText(TestData.PathTo("read_path_golden.json")))!;

    private static string Sha256Hex(byte[] bytes) => Convert.ToHexStringLower(SHA256.HashData(bytes));

    [Fact]
    public void TheKeyIsSha256OfRuleIdNulAndTheFullMatch()
    {
        var expected = Sha256Hex([.. "cred.aws_access_key\0"u8, .. Encoding.UTF8.GetBytes(KeyA)]);
        Assert.Equal(expected, Provide.Uterm.Annotation.Annotation.ComputeMatchKey("cred.aws_access_key", KeyA));
        Assert.Equal(64, expected.Length);
    }

    [Fact]
    public void KeysMatchTheReference()
    {
        var cases = Golden["match_keys"]!.AsArray();
        Assert.NotEmpty(cases);
        foreach (var c in cases)
        {
            Assert.Equal(
                (string)c!["key"]!,
                Provide.Uterm.Annotation.Annotation.ComputeMatchKey((string)c["rule_id"]!, (string)c["match"]!));
        }
    }

    [Fact]
    public void TheDetectorsKeysMatchTheReference()
    {
        var cases = Golden["detected_match_keys"]!.AsArray();
        Assert.NotEmpty(cases);
        foreach (var c in cases)
        {
            var want = c!["annotations"]!.AsArray()
                .Select(a => $"{(string)a!["label"]!}|{(string)a["description"]!}|{(string)a["match_key"]!}");
            var got = new PatternDetector().Detect("read", (string)c["text"]!, 1)
                .Select(a => $"{a.Label}|{a.Description}|{a.MatchKey}");
            Assert.Equal(want, got);
        }
    }

    [Fact]
    public void ALoneSurrogateHashesAsPythonsSurrogatepass()
    {
        // "\ud800".encode("utf-8", "surrogatepass") == b"\xed\xa0\x80"
        Assert.Equal(
            Sha256Hex([(byte)'r', 0, 0xED, 0xA0, 0x80]),
            Provide.Uterm.Annotation.Annotation.ComputeMatchKey("r", "\ud800"));
        Assert.Equal(
            Sha256Hex([(byte)'r', 0, 0xED, 0xB0, 0x80, (byte)'x', 0xED, 0xA0, 0x80]),
            Provide.Uterm.Annotation.Annotation.ComputeMatchKey("r", "\udc00x\ud800"));
    }

    [Theory]
    [InlineData("a")]
    [InlineData("\u007f")]
    [InlineData("\u0080")]
    [InlineData("߿")]
    [InlineData("ࠀ")]
    [InlineData("￿")]
    [InlineData("\U00010000")]
    [InlineData("\U0010ffff")]
    [InlineData("café ☃ \U0001f600")]
    public void WellFormedTextHashesAsUtf8(string match)
    {
        Assert.Equal(
            Sha256Hex([.. Encoding.UTF8.GetBytes("r"), 0, .. Encoding.UTF8.GetBytes(match)]),
            Provide.Uterm.Annotation.Annotation.ComputeMatchKey("r", match));
    }

    [Fact]
    public void TwoDifferentCredentialsOfOneRuleGetDifferentKeys()
    {
        var detector = new PatternDetector();
        var first = Assert.Single(detector.Detect("read", $"export K={KeyA}", 1));
        var second = Assert.Single(detector.Detect("read", $"export K={KeyB}", 2));
        // Same rule, same description -- only the match key tells them apart.
        Assert.Equal(first.Description, second.Description);
        Assert.Equal(
            Provide.Uterm.Annotation.Annotation.ComputeMatchKey("cred.aws_access_key", "AKIAIOSFODNN7EXA"), first.MatchKey);
        Assert.Equal(
            Provide.Uterm.Annotation.Annotation.ComputeMatchKey("cred.aws_access_key", "AKIAJ2345678ABCD"), second.MatchKey);
        Assert.NotEqual(first.MatchKey, second.MatchKey);
    }

    [Fact]
    public void TheSameMatchGetsTheSameKeyWhereverItAppears()
    {
        var detector = new PatternDetector();
        var first = Assert.Single(detector.Detect("read", "$ sudo ls", 1));
        var second = Assert.Single(detector.Detect("send", "later: sudo ls -l", 9));
        Assert.Equal(first.MatchKey, second.MatchKey);
    }

    [Fact]
    public void TheKeyHashesTheFullMatchNotTheTruncatedDescription()
    {
        var longMatch = "curl " + new string('a', 200) + " https://";
        var annotation = Assert.Single(new PatternDetector().Detect("send", longMatch + "x", 1));
        Assert.Equal(Provide.Uterm.Annotation.Annotation.ComputeMatchKey("conn.curl", longMatch), annotation.MatchKey);

        // Same first 80 characters, so the same description; a different match.
        var other = Assert.Single(
            new PatternDetector().Detect("send", "curl " + new string('a', 199) + "b https://x", 1));
        Assert.Equal(annotation.Description, other.Description);
        Assert.NotEqual(annotation.MatchKey, other.MatchKey);
    }

    [Fact]
    public void TheKeyIsNotSerialised()
    {
        var annotation = Assert.Single(new PatternDetector().Detect("read", KeyA, 1));
        Assert.Equal(64, annotation.MatchKey.Length);
        Assert.DoesNotContain("match_key", annotation.ToDict().Keys);
        var json = JsonSerializer.Serialize(annotation);
        Assert.DoesNotContain(annotation.MatchKey, json, StringComparison.Ordinal);
        Assert.DoesNotContain("MatchKey", json, StringComparison.Ordinal);
        Assert.DoesNotContain(KeyA, JsonSerializer.Serialize(annotation.ToDict()), StringComparison.Ordinal);
        Assert.Equal("", new Provide.Uterm.Annotation.Annotation().MatchKey);
    }

    private static string Key(string ruleId, string match) =>
        Provide.Uterm.Annotation.Annotation.ComputeMatchKey(ruleId, match);

    [Fact]
    public void ALoneSurrogateIsFound()
    {
        // A Fact, not InlineData: attribute strings are stored as UTF-8, which
        // turns a lone surrogate into U+FFFD before the test ever sees it.
        (string Text, bool Lone)[] cases =
        [
            ("", false),
            ("plain", false),
            ("\U0001f600", false),
            ("a\U0001f600b\U0010ffff", false),
            ("\ud800", true),
            ("\udc00", true),
            ("a\ud800", true),
            ("\ud800a", true),
            ("\udc00\ud800", true),
            ("\ud800\ud800\udc00", true),
            ("\U0001f600\udc00", true),
        ];
        foreach (var (text, lone) in cases)
        {
            Assert.True(
                lone == Provide.Uterm.Annotation.Annotation.HasLoneSurrogate(text),
                $"{string.Join(",", text.Select(c => ((int)c).ToString("x4")))}: expected {lone}");
        }
    }

    public static TheoryData<string> WellFormed => new()
    {
        "",
        "AKIAIOSFODNN7EXAMPLE",
        "caf\u00e9 \u2603 \U0001f600 \u07ff\u0800\uffff\U00010000\U0010ffff",  // codespell:ignore caf
        "\u007f\u0080", // either side of the one-byte / two-byte boundary
        new string('\u2603', 170), // 510 bytes + rule + NUL: just over the stack buffer
        new string('x', Provide.Uterm.Annotation.Annotation.MatchKeyStackLimit - 2), // exactly at it
        new string('x', Provide.Uterm.Annotation.Annotation.MatchKeyStackLimit - 1), // one past it
        string.Concat(Enumerable.Repeat("\U0001f600", 300)),
    };

    [Theory]
    [MemberData(nameof(WellFormed))]
    public void TheFastPathHashesTheSameBytesAsSurrogatepass(string match)
    {
        // For well-formed text, surrogatepass is plain UTF-8: both encoders agree.
        var surrogatePass = Provide.Uterm.Annotation.Annotation.MatchKeyInputSurrogatePass("r", match);
        Assert.Equal([.. "r\0"u8, .. Encoding.UTF8.GetBytes(match)], surrogatePass);
        Assert.Equal(Sha256Hex(surrogatePass), Key("r", match));
    }

    [Fact]
    public void TheSlowPathIsTakenForALoneSurrogateInEitherPart()
    {
        // Long enough that a buffer bug would show; the lone surrogate encodes as
        // its own three bytes (Encoding.UTF8 would write EF BF BD instead).
        var longMatch = new string('x', 600) + "\ud800";
        Assert.Equal(
            Sha256Hex([(byte)'r', 0, .. Encoding.ASCII.GetBytes(new string('x', 600)), 0xED, 0xA0, 0x80]),
            Key("r", longMatch));
        Assert.Equal(Sha256Hex([0xED, 0xB0, 0x80, 0, (byte)'m']), Key("\udc00", "m"));
        Assert.NotEqual(Sha256Hex([.. Encoding.UTF8.GetBytes("r\0\ud800")]), Key("r", "\ud800"));
    }

    [Fact]
    public void KeysAreStableAcrossRepeatedCallsOnBothPaths()
    {
        var big = new string('y', 4000);
        Assert.Equal(Key("r", big), Key("r", big));
        Assert.Equal(Key("r", big + "\ud800"), Key("r", big + "\ud800"));
        Assert.NotEqual(Key("r", big), Key("r", big + "z"));
    }
}
