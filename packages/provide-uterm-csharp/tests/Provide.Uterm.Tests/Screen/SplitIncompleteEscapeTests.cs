//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.RegularExpressions;
using Provide.Uterm.Screen;

namespace Provide.Uterm.Tests.Screen;

/// <summary>
/// <see cref="ScreenNormalize.SplitIncompleteEscape"/> carries exactly what
/// <see cref="ScreenNormalize.StripAnsi"/> would strip once complete: both are
/// built from the same grammar constants. Parity with the reference's
/// <c>provide-uterm/tests/terminal/test_split_incomplete_escape.py</c> — every
/// sequence StripAnsi strips (up to <see cref="ScreenNormalize.MaxEscapeCarry"/>),
/// cut at every position, strips to the same text when the first part is split
/// and its carry is prepended to the rest.
/// </summary>
public sealed class SplitIncompleteEscapeTests
{
    private const string Prefix = "ab";

    // Neither a CSI parameter, intermediate nor final byte, so the sequence
    // under test ends where it is meant to and nothing after it is pulled in.
    private const string Suffix = "\n!";

    private static readonly Regex Strippable = new("^(?:" + ScreenNormalize.AnsiEscapePattern + @")\z");

    private static IEnumerable<string> FeSequences() =>
        Enumerable.Range(0x40, 0x20).Select(c => "\u001b" + (char)c);

    private static IEnumerable<string> CsiSequences()
    {
        string[] parameters = ["", "1", "38;2;255;0;0", "?25", "<=>", "0;1:2"];
        string[] intermediates = ["", " ", "!", "/ "];
        var finals = Enumerable.Range(0x40, 0x3F).Select(c => ((char)c).ToString()).ToList();
        return from p in parameters from i in intermediates from f in finals select "\u001b[" + p + i + f;
    }

    private static void AssertCarriedCorrectly(string seq)
    {
        Assert.Matches(Strippable, seq);
        var whole = Prefix + seq + Suffix;
        var expected = ScreenNormalize.StripAnsi(whole);
        Assert.Equal(Prefix + Suffix, expected);
        for (var cut = 0; cut <= seq.Length; cut++)
        {
            var head = Prefix + seq[..cut];
            var (complete, carry) = ScreenNormalize.SplitIncompleteEscape(head);
            Assert.Equal(head, complete + carry);
            if (cut > 0 && cut < seq.Length) Assert.Equal(seq[..cut], carry);
            if (cut == 0) Assert.Equal("", carry);
            Assert.Equal(
                expected,
                ScreenNormalize.StripAnsi(complete) + ScreenNormalize.StripAnsi(carry + seq[cut..] + Suffix));
        }
    }

    [Fact]
    public void EveryEnumeratedSequenceIsCarriedCorrectlyAtEveryCut()
    {
        var sequences = FeSequences().Concat(CsiSequences()).ToList();
        Assert.Equal(32 + (6 * 4 * 63), sequences.Count);
        foreach (var seq in sequences) AssertCarriedCorrectly(seq);
    }

    [Fact]
    public void AnySequenceTheGrammarGeneratesIsCarriedCorrectlyAtEveryCut()
    {
        // The reference draws these with hypothesis from the strip pattern; this
        // draws from the same grammar with a fixed seed.
        var random = new Random(20261010);
        for (var n = 0; n < 2000; n++)
        {
            string seq;
            if (random.Next(4) == 0)
            {
                seq = "\u001b" + (char)random.Next(0x40, 0x60);
            }
            else
            {
                var sb = new StringBuilder("\u001b[");
                var parameters = random.Next(0, 40);
                for (var i = 0; i < parameters; i++) sb.Append((char)random.Next(0x30, 0x40));
                var intermediates = random.Next(0, 6);
                for (var i = 0; i < intermediates; i++) sb.Append((char)random.Next(0x20, 0x30));
                sb.Append((char)random.Next(0x40, 0x7F));
                seq = sb.ToString();
            }

            if (seq.Length > ScreenNormalize.MaxEscapeCarry) continue;
            AssertCarriedCorrectly(seq);
        }
    }

    [Theory]
    [InlineData("", "", "")]
    [InlineData("plain", "plain", "")]
    [InlineData("a\u001b", "a", "\u001b")]
    [InlineData("a\u001b[38;2;255", "a", "\u001b[38;2;255")]
    [InlineData("a\u001b[1mb", "a\u001b[1mb", "")]
    [InlineData("a\u001bc", "a\u001bc", "")]
    [InlineData("\u001b[1m\u001b[3", "\u001b[1m", "\u001b[3")]
    public void Examples(string text, string complete, string carry)
    {
        Assert.Equal((complete, carry), ScreenNormalize.SplitIncompleteEscape(text));
    }

    [Fact]
    public void TheCarryIsBoundedByMaxEscapeCarry()
    {
        var fits = "\u001b[" + new string('1', ScreenNormalize.MaxEscapeCarry - 2);
        Assert.Equal(("", fits), ScreenNormalize.SplitIncompleteEscape(fits));
        var over = "\u001b[" + new string('1', ScreenNormalize.MaxEscapeCarry - 1);
        Assert.Equal((over, ""), ScreenNormalize.SplitIncompleteEscape(over));
    }

    [Fact]
    public void MaxCarryIsConfigurableAndDefaultsTo64()
    {
        Assert.Equal(64, ScreenNormalize.MaxEscapeCarry);
        Assert.Equal(("x", "\u001b[12"), ScreenNormalize.SplitIncompleteEscape("x\u001b[12", maxCarry: 4));
        Assert.Equal(("x\u001b[123", ""), ScreenNormalize.SplitIncompleteEscape("x\u001b[123", maxCarry: 4));
    }

    [Fact]
    public void BothPatternsAreBuiltFromTheSharedGrammar()
    {
        Assert.Equal("\u001b(?:\\[[0-?]*[ -/]*[@-~]|[@-_])", ScreenNormalize.AnsiEscapePattern);
        Assert.Equal("^\u001b(?:\\[[0-?]*[ -/]*)?\\z", ScreenNormalize.IncompleteEscapePattern);
    }
}
