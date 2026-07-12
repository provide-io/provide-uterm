//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Sanitizer;

namespace Provide.Uterm.Tests;

public class SanitizerTests
{
    [Fact]
    public void UnescapeKeys_SimpleEscapes()
    {
        Assert.Equal("a\nb", KeystrokeSanitizer.UnescapeKeys(@"a\nb"));
        Assert.Equal("\x1b[A", KeystrokeSanitizer.UnescapeKeys(@"\e[A"));
        Assert.Equal("\t", KeystrokeSanitizer.UnescapeKeys(@"\t"));
        Assert.Equal("\\", KeystrokeSanitizer.UnescapeKeys(@"\\"));
    }

    [Fact]
    public void UnescapeKeys_HexAndUnicode()
    {
        Assert.Equal("A", KeystrokeSanitizer.UnescapeKeys(@"\x41"));
        Assert.Equal("A", KeystrokeSanitizer.UnescapeKeys(@"\u0041"));
    }

    [Fact]
    public void SanitizeKeystrokes_StripsNonPrintable()
    {
        Assert.Equal("hi", KeystrokeSanitizer.SanitizeKeystrokes("h\x00i"));
        Assert.Equal("ok\r\n", KeystrokeSanitizer.SanitizeKeystrokes("ok\r\n"));
        Assert.Equal("\x1b", KeystrokeSanitizer.SanitizeKeystrokes("\x1b"));
    }

    [Fact]
    public void SanitizeKeystrokes_Truncates()
    {
        Assert.Equal("ab", KeystrokeSanitizer.SanitizeKeystrokes("abcdef", maxBytes: 2));
    }

    [Fact]
    public void PrepareKeystrokes_UnescapesThenSanitizes()
    {
        Assert.Equal("a\nb", KeystrokeSanitizer.PrepareKeystrokes(@"a\nb"));
    }
}
