//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Screen;

namespace Provide.Uterm.Tests.Screen;

public class ScreenModuleTests
{
    [Fact]
    public void Cp437_RoundTrip_Ascii()
    {
        var text = "Hello CP437!";
        var encoded = Cp437.Encode(text);
        Assert.Equal(Encoding.ASCII.GetBytes(text), encoded);
        Assert.Equal(text, Cp437.Decode(encoded));
    }

    [Fact]
    public void Cp437_Encode_UnknownBecomesQuestion()
    {
        // U+4E2D (中) is not in CP437
        var bytes = Cp437.Encode("中");
        Assert.Equal(new byte[] { (byte)'?' }, bytes);
    }

    [Fact]
    public void Cp437_Decode_HighBytes()
    {
        // 0xB0 is light shade ░ in CP437
        var s = Cp437.Decode(new byte[] { 0xB0, 0x41 });
        Assert.EndsWith("A", s, StringComparison.Ordinal);
        Assert.Equal(2, s.EnumerateRunes().Count());
    }

    [Fact]
    public void Normalize_StripsAnsiAndBareSgr()
    {
        Assert.Equal("", ScreenNormalize.NormalizeTerminalText(""));
        var cleaned = ScreenNormalize.NormalizeTerminalText("hi\x1b[31mRED\x1b[0m\r\nnext");
        Assert.DoesNotContain("\x1b", cleaned, StringComparison.Ordinal);
        Assert.Contains("RED", cleaned, StringComparison.Ordinal);
        Assert.Contains("next", cleaned, StringComparison.Ordinal);
        Assert.Equal(cleaned, ScreenNormalize.StripAnsi("hi\x1b[31mRED\x1b[0m\r\nnext"));
    }

    [Fact]
    public void Normalize_ExtractActionTags()
    {
        var tags = ScreenNormalize.ExtractActionTags("<Attack> <Defend> <Attack> <Skip>");
        Assert.Equal(new[] { "Attack", "Defend", "Skip" }, tags);

        var limited = ScreenNormalize.ExtractActionTags("<A> <B> <C>", maxTags: 2);
        Assert.Equal(2, limited.Count);

        Assert.Empty(ScreenNormalize.ExtractActionTags(""));
        Assert.Single(ScreenNormalize.ExtractActionTags("<Only>", maxTags: 0));
    }

    [Fact]
    public void Normalize_CleanScreenForDisplay()
    {
        var pad = new string(' ', 80);
        var screen = "visible\n" + pad + "\nline2";
        var lines = ScreenNormalize.CleanScreenForDisplay(screen, maxLines: 10);
        Assert.Contains("visible", lines);
        Assert.Contains("line2", lines);
    }

    [Fact]
    public void Extract_MenuOptions_Default()
    {
        var screen = "<A> Attack\n<B> Block\n(C) Cast";
        var opts = Extract.ExtractMenuOptions(screen);
        Assert.True(opts.Count >= 2);
        Assert.Contains(opts, o => o.Key == "A" && o.Description.Contains("Attack", StringComparison.Ordinal));
    }

    [Fact]
    public void Extract_MenuOptions_CustomPattern()
    {
        var screen = "1) One\n2) Two";
        var opts = Extract.ExtractMenuOptions(screen, @"(\d+)\)\s+(.+)");
        Assert.Equal(2, opts.Count);
        Assert.Equal("1", opts[0].Key);
        Assert.Equal("One", opts[0].Description);
    }

    [Fact]
    public void Extract_MenuOptions_InvalidPattern_Empty()
    {
        Assert.Empty(Extract.ExtractMenuOptions("x", "["));
    }

    [Fact]
    public void Extract_NumberedList()
    {
        var screen = "  1. First item\n  2) Second item\nnope";
        var items = Extract.ExtractNumberedList(screen);
        Assert.Equal(2, items.Count);
        Assert.Equal("1", items[0].Number);
        Assert.Equal("First item", items[0].Description);
        Assert.Equal("2", items[1].Number);
    }

    [Fact]
    public void Extract_NumberedList_CustomPattern()
    {
        var items = Extract.ExtractNumberedList("#1 foo\n#2 bar", @"#(\d+)\s+(.+)");
        Assert.Equal(2, items.Count);
        Assert.Equal("1", items[0].Number);
        Assert.Equal("foo", items[0].Description);
    }

    [Fact]
    public void Extract_KeyValuePairs()
    {
        var screen = "Name: Alice\nScore: 42";
        var kv = Extract.ExtractKeyValuePairs(screen, new Dictionary<string, string>
        {
            ["name"] = @"Name:\s*(\w+)",
            ["score"] = @"Score:\s*(\d+)",
            ["missing"] = @"Nope:\s*(\w+)",
        });
        Assert.Equal("Alice", kv["name"]);
        Assert.Equal("42", kv["score"]);
        Assert.False(kv.ContainsKey("missing"));
    }
}
