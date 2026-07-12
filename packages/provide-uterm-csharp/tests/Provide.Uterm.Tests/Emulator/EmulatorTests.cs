//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Emulator;

namespace Provide.Uterm.Tests.Emulator;

public class EmulatorTests
{
    [Fact]
    public void Process_PlainText_AppearsInSnapshot()
    {
        var emu = new TerminalEmulator(80, 24);
        emu.Process(Encoding.UTF8.GetBytes("hello world"));
        var snap = emu.GetSnapshot();
        Assert.Contains("hello world", snap.Screen, StringComparison.Ordinal);
        Assert.False(string.IsNullOrEmpty(snap.ScreenHash));
        Assert.Equal(80, snap.Cols);
        Assert.Equal(24, snap.Rows);
    }

    [Fact]
    public void ANSIScreen_ReturnsEscapes()
    {
        var emu = new TerminalEmulator(40, 10);
        emu.Process(Encoding.UTF8.GetBytes("x"));
        var ansi = emu.ANSIScreen();
        Assert.False(string.IsNullOrEmpty(ansi));
    }

    [Fact]
    public void Reset_ClearsContent()
    {
        var emu = new TerminalEmulator(20, 5);
        emu.Process(Encoding.UTF8.GetBytes("abc"));
        emu.Reset();
        var snap = emu.GetSnapshot();
        Assert.DoesNotContain("abc", snap.Screen, StringComparison.Ordinal);
    }

    [Fact]
    public void Resize_UpdatesDimensions()
    {
        var emu = new TerminalEmulator(80, 24);
        emu.Resize(40, 12);
        Assert.Equal(40, emu.Cols);
        Assert.Equal(12, emu.Rows);
    }
}
