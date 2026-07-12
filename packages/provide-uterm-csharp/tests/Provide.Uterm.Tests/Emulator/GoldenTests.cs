//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Emulator;

namespace Provide.Uterm.Tests.Emulator;

public class GoldenTests
{
    [Fact]
    public void GoldenParityWithPython()
    {
        var path = TestData.PathTo("emulator", "python_golden.json");
        Assert.True(File.Exists(path), path);
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var cases = doc.RootElement.EnumerateArray().ToList();
        Assert.NotEmpty(cases);
        for (var i = 0; i < cases.Count; i++)
        {
            var c = cases[i];
            var data = Convert.FromHexString(c.GetProperty("raw_hex").GetString()!);
            var e = new TerminalEmulator(40, 6, "");
            e.Process(data);
            var snap = e.GetSnapshot();
            Assert.Equal(c.GetProperty("screen").GetString(), snap.Screen);
            Assert.Equal(c.GetProperty("hash").GetString(), snap.ScreenHash);
            Assert.Equal(c.GetProperty("cursor").GetProperty("x").GetInt32(), snap.Cursor.X);
            Assert.Equal(c.GetProperty("cursor").GetProperty("y").GetInt32(), snap.Cursor.Y);
            Assert.Equal(c.GetProperty("cae").GetBoolean(), snap.CursorAtEnd);
            Assert.Equal(c.GetProperty("hts").GetBoolean(), snap.HasTrailingSpace);
            Assert.Equal(c.GetProperty("raw_tail").GetString(), snap.RawTail);
            var ansi0 = e.AnsiScreen().Split('\n')[0];
            Assert.Equal(c.GetProperty("ansi0").GetString(), ansi0);
        }
    }

    [Fact]
    public void DefaultsAndAccessors()
    {
        var e = new TerminalEmulator(0, 0, "");
        Assert.Equal(80, e.Cols);
        Assert.Equal(25, e.Rows);
        var snap = e.GetSnapshot();
        Assert.Equal("ANSI", snap.Term);
        Assert.Equal(80, snap.Cols);
        Assert.True(snap.CapturedAt > 0);
    }
}
