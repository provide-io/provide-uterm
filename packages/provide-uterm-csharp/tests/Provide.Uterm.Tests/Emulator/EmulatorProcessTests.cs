//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Emulator;

namespace Provide.Uterm.Tests.Emulator;

public class EmulatorProcessTests
{
    [Fact]
    public void Process_PlainText_AppearsInSnapshot()
    {
        var emu = new TerminalEmulator(40, 10, "xterm");
        emu.Process(Encoding.UTF8.GetBytes("Hello World"));
        var snap = emu.GetSnapshot();
        Assert.Contains("Hello World", snap.Screen, StringComparison.Ordinal);
        Assert.Equal(40, snap.Cols);
        Assert.Equal(10, snap.Rows);
        Assert.Equal("xterm", snap.Term);
        Assert.False(string.IsNullOrEmpty(snap.ScreenHash));
        Assert.Equal(snap.ScreenHash, emu.GetSnapshot().ScreenHash); // cached while clean
        Assert.Contains("Hello World", emu.RawTail, StringComparison.Ordinal);
    }

    [Fact]
    public void Process_AnsiColors_And_AnsiScreen()
    {
        var emu = new TerminalEmulator();
        emu.Process(Encoding.ASCII.GetBytes("\x1b[31mRED\x1b[0m plain"));
        var snap = emu.GetSnapshot();
        Assert.Contains("RED", snap.Screen, StringComparison.Ordinal);
        Assert.Contains("plain", snap.Screen, StringComparison.Ordinal);
        var ansi = emu.AnsiScreen();
        Assert.Equal(ansi, emu.ANSIScreen());
        Assert.NotNull(emu.Screen);
        Assert.Equal(80, emu.Cols);
        Assert.Equal(25, emu.Rows);
    }

    [Fact]
    public void Process_CsiCursorAndClear()
    {
        var emu = new TerminalEmulator(20, 5);
        emu.Process(Encoding.ASCII.GetBytes("AAAA\x1b[1;1HBB"));
        var snap = emu.GetSnapshot();
        Assert.Contains("BB", snap.Screen, StringComparison.Ordinal);
        emu.Process(Encoding.ASCII.GetBytes("\x1b[2J\x1b[H"));
        snap = emu.GetSnapshot();
        Assert.DoesNotContain("AAAA", snap.Screen, StringComparison.Ordinal);
    }

    [Fact]
    public void Resize_And_Reset()
    {
        var emu = new TerminalEmulator(10, 5);
        emu.Process(Encoding.ASCII.GetBytes("abc"));
        emu.Resize(30, 12);
        Assert.Equal(30, emu.Cols);
        Assert.Equal(12, emu.Rows);
        emu.Reset();
        var snap = emu.GetSnapshot();
        Assert.DoesNotContain("abc", snap.Screen.Trim(), StringComparison.Ordinal);
    }

    [Fact]
    public void Process_NewlineAndCursorPosition()
    {
        var emu = new TerminalEmulator(40, 8);
        emu.Process(Encoding.ASCII.GetBytes("line1\r\nline2"));
        var snap = emu.GetSnapshot();
        Assert.Contains("line1", snap.Screen, StringComparison.Ordinal);
        Assert.Contains("line2", snap.Screen, StringComparison.Ordinal);
        Assert.True(snap.Cursor.Y >= 1 || snap.Cursor.X > 0);
    }

    [Fact]
    public void Process_Cp437Bytes()
    {
        var emu = new TerminalEmulator(40, 5);
        // 0x01 is smiling face in CP437 → U+0001 control; 0x41 is 'A'
        emu.Process(new byte[] { 0x41, 0x42, 0x43 });
        Assert.Contains("ABC", emu.GetSnapshot().Screen, StringComparison.Ordinal);
    }

    [Fact]
    public void Process_SgrAttributes_BoldUnderline()
    {
        var emu = new TerminalEmulator(40, 5);
        emu.Process(Encoding.ASCII.GetBytes("\x1b[1;4mBOLD\x1b[0m"));
        Assert.Contains("BOLD", emu.GetSnapshot().Screen, StringComparison.Ordinal);
        var ansi = emu.AnsiScreen();
        Assert.NotEmpty(ansi);
    }

    [Fact]
    public void Defaults_WhenZeroColsRows()
    {
        var emu = new TerminalEmulator(0, 0, "");
        Assert.Equal(80, emu.Cols);
        Assert.Equal(25, emu.Rows);
    }
}
