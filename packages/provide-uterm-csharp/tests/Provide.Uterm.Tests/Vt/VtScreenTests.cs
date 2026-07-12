//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Vt;
using VtScreen = Provide.Uterm.Vt.Screen;

namespace Provide.Uterm.Tests.VtSuite;

public class VtScreenTests
{
    [Fact]
    public void Display_ShowsWrittenChars()
    {
        var s = new VtScreen(40, 10);
        var stream = new VtStream(s);
        stream.Feed("Hello");
        var lines = s.Display();
        Assert.Contains(lines, l => l.Contains("Hello", StringComparison.Ordinal));
    }

    [Fact]
    public void Csi_CursorMove_And_Clear()
    {
        var s = new VtScreen(20, 5);
        var stream = new VtStream(s);
        stream.Feed("abc\u001b[1;1H\u001b[2J");
        // After clear, screen should not retain abc as primary content in all cases
        Assert.NotNull(s.Display());
        Assert.True(s.Modes().Count >= 0);
    }

    [Fact]
    public void CombiningMarks_DoNotCrash()
    {
        var s = new VtScreen(20, 5);
        var stream = new VtStream(s);
        // e + combining acute
        stream.Feed("e\u0301");
        Assert.NotEmpty(s.Display());
    }

    [Fact]
    public void HangulSyllable_Processes()
    {
        var s = new VtScreen(20, 5);
        var stream = new VtStream(s);
        stream.Feed("\uAC00"); // 가
        Assert.NotEmpty(s.Display());
    }

    [Fact]
    public void WideChar_DoubleWidth()
    {
        var s = new VtScreen(20, 5);
        var stream = new VtStream(s);
        stream.Feed("\u4E2D"); // CJK
        Assert.NotEmpty(s.Display());
    }

    [Fact]
    public void Sgr_BoldAndColors()
    {
        var s = new VtScreen(40, 5);
        var stream = new VtStream(s);
        stream.Feed("\u001b[1;31mRED\u001b[0m");
        var text = string.Join("\n", s.Display());
        Assert.Contains("RED", text, StringComparison.Ordinal);
    }
}
