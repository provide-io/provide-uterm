//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.RegularExpressions;
using Provide.Uterm.Emulator;
using Provide.Uterm.Render;
using Provide.Uterm.Screen;
using Provide.Uterm.Vt;

namespace Provide.Uterm.Tests.Emulator;

/// <summary>
/// A snapshot must leave a viewer's terminal where the live stream expects it.
/// Port of test_emulator_snapshot_resume.py: snapshot at a split point, paint a
/// fresh emulator with <see cref="TerminalEmulator.AnsiScreen"/>, feed the
/// remainder, and the result must equal the uninterrupted stream's. A program
/// that paints with relative movement (vitetris never sends CUP) drew its whole
/// board in the wrong place when the snapshot left the cursor where the painted
/// rows ended.
/// </summary>
public class SnapshotResumeTests
{
    private const string FixtureRelativePath = "conformance/snapshot/relative_paint_80x24.bin";

    /// <summary>
    /// One terminal token. Splits fall between tokens: a viewer handed half an
    /// escape sequence would print the other half whatever the snapshot said.
    /// </summary>
    private static readonly Regex Token = new(
        @"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|.",
        RegexOptions.Singleline | RegexOptions.CultureInvariant);

    private static byte[] Fixture()
    {
        var parts = FixtureRelativePath.Split('/');
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return File.ReadAllBytes(candidate);
            }
        }

        throw new FileNotFoundException("snapshot fixture not found above " + AppContext.BaseDirectory, FixtureRelativePath);
    }

    private static List<int> SplitPoints(byte[] data) =>
        Token.Matches(Encoding.Latin1.GetString(data)).Select(m => m.Index + m.Length).ToList();

    private static byte[] Bytes(string text) => Encoding.Latin1.GetBytes(text);

    private static TerminalEmulator Fed(int cols, int rows, string stream)
    {
        var e = new TerminalEmulator(cols, rows, "");
        e.Process(Bytes(stream));
        return e;
    }

    private static (string Screen, int X, int Y, string Ansi) StateOf(TerminalEmulator e)
    {
        var snap = e.GetSnapshot();
        return (snap.Screen, snap.Cursor.X, snap.Cursor.Y, e.AnsiScreen());
    }

    private static void AssertResumes(byte[] data, int split, int cols, int rows)
    {
        var whole = new TerminalEmulator(cols, rows, "");
        whole.Process(data);
        var live = new TerminalEmulator(cols, rows, "");
        live.Process(data.AsSpan(0, split));
        var viewer = new TerminalEmulator(cols, rows, "");
        // What the browser element writes for a snapshot frame's screen,
        // encoded back to the CP437 bytes this emulator decodes.
        viewer.Process(Cp437.Encode(live.AnsiScreen().Replace("\n", "\r\n", StringComparison.Ordinal)));
        viewer.Process(data.AsSpan(split));
        Assert.True(StateOf(viewer) == StateOf(whole), $"split at byte {split}");
    }

    private static string SuffixOf(TerminalEmulator e)
    {
        var ansi = e.AnsiScreen();
        return ansi[(ansi.LastIndexOf('\n') + 1)..];
    }

    private static Vt.Screen CharsetScreen(int cols, int rows, string text)
    {
        var screen = new Vt.Screen(cols, rows);
        new VtStream(screen) { UseUtf8 = false }.Feed(text);
        return screen;
    }

    [Fact]
    public void ResumesTheRelativePaintFixtureAtEveryTokenBoundary()
    {
        var data = Fixture();
        Assert.Equal(4634, data.Length);
        var points = SplitPoints(data);
        Assert.True(points.Count > 1500);
        foreach (var split in points.Prepend(0))
        {
            AssertResumes(data, split, 80, 24);
        }
    }

    [Fact]
    public void RestoresCursorAndPen()
    {
        const string stream = "top\r\n\x1b[2Bmid\x1b[3D";
        Assert.Contains("\x1b[4;1H", SuffixOf(Fed(20, 5, stream)), StringComparison.Ordinal);
        AssertResumes(Bytes(stream + "\x1b[1Aup"), stream.Length, 20, 5);
        Assert.EndsWith("\x1b[?25l", SuffixOf(Fed(20, 5, "\x1b[?25l")), StringComparison.Ordinal);
        Assert.Contains(
            "\x1b[0;1;3;4;5;7;9;38;2;255;135;0;48;2;1;2;3m",
            SuffixOf(Fed(20, 5, "\x1b[1;3;4;5;7;9;38;5;208;48;2;1;2;3mX")),
            StringComparison.Ordinal);
        const string before = "\x1b[44;1;37m\x1b[5C";
        AssertResumes(Bytes(before + "\x1b[1Bboard"), before.Length, 20, 5);
        Assert.Equal("\x1b[0m", TerminalState.PenSgr(default));
        Assert.Equal("\x1b[0;33;103m", TerminalState.PenSgr(new Vt.Char { FG = "brown", BG = "brightbrown" }));
    }

    [Fact]
    public void DrawsItalicAndStruckCells()
    {
        // The pen carried them, but a drawn cell lost them: a viewer that
        // joined saw plain text where everyone else saw italic or struck text.
        Assert.StartsWith(
            "\x1b[0;3mI\x1b[0;9mS\x1b[0;1;3;4;5;7;9mA\x1b[0m ",
            Fed(10, 2, "\x1b[3mI\x1b[0;9mS\x1b[0;1;3;4;5;7;9mA\x1b[m").AnsiScreen(),
            StringComparison.Ordinal);
        const string before = "\x1b[3mit\x1b[23;9mst\x1b[m plain";
        AssertResumes(Bytes(before + "\x1b[3m!"), before.Length, 20, 5);
        Assert.Equal(
            "\x1b[0;1;3;4;5;7;9;31m",
            RenderBuffer.StyleToSgr(new RenderBuffer.Style
            {
                FG = "red", BG = "default", Bold = true, Italics = true, Underscore = true,
                Blink = true, Reverse = true, Strikethrough = true,
            }));
        var italic = new RenderBuffer.Style { FG = "default", BG = "default", Italics = true };
        Assert.NotEqual(italic, RenderBuffer.DefaultStyle);
        Assert.NotEqual(new RenderBuffer.Style { FG = "default", BG = "default", Strikethrough = true }, italic);
    }

    [Fact]
    public void RestoresTheScrollRegionAndOriginMode()
    {
        Assert.Contains(
            "\x1b[3;7r\x1b[?6h\x1b[?7h\x1b[4l\x1b[2;4H",
            SuffixOf(Fed(20, 10, "\x1b[3;7r\x1b[?6h\x1b[2;4H")),
            StringComparison.Ordinal);
        Assert.Contains(" \x1b[0m\x1b[r\x1b[?6l", SuffixOf(Fed(20, 10, "\x1b[1;10r")), StringComparison.Ordinal);
        Assert.Contains("\x1b[2;4r\x1b[?6l", SuffixOf(Fed(20, 10, "\x1b[2;4r\x1b[3;3H")), StringComparison.Ordinal);
        foreach (var origin in new[] { "", "\x1b[?6h" })
        {
            var stream = Bytes("line1\r\n\x1b[2;5r" + origin + "\x1b[3Bin\r\n\r\n\r\nscrolled\r\nagain");
            foreach (var split in SplitPoints(stream))
            {
                AssertResumes(stream, split, 20, 8);
            }
        }
    }

    [Fact]
    public void KeepsTheDeferredWrap()
    {
        var stream = "\x1b[2;1H" + new string('x', 19) + "\x1b[32mY";
        var live = Fed(20, 4, stream);
        Assert.Equal((20, 1), (live.GetSnapshot().Cursor.X, live.GetSnapshot().Cursor.Y));
        Assert.Contains("\x1b[2;20H\x1b[0;32mY", SuffixOf(live), StringComparison.Ordinal);
        var full = Bytes(stream + "\x1b[35mwrapped");
        foreach (var split in SplitPoints(full))
        {
            AssertResumes(full, split, 20, 4);
        }

        var origin = "\x1b[2;4r\x1b[?6h\x1b[2;1H" + new string('z', 20);
        Assert.Contains("\x1b[2;20H", SuffixOf(Fed(20, 6, origin)), StringComparison.Ordinal);
        AssertResumes(Bytes(origin + "next"), origin.Length, 20, 6);
        var noWrap = "\x1b[?7l" + new string('a', 20);
        Assert.Contains("\x1b[?7l", SuffixOf(Fed(20, 3, noWrap)), StringComparison.Ordinal);
        AssertResumes(Bytes(noWrap + "bc"), noWrap.Length, 20, 3);
        // A wide character in the last two columns is redrawn whole.
        Assert.Contains(
            "\x1b[1;19H\x1b[0m中",
            TerminalState.ScreenStateSuffix(CharsetScreen(20, 3, "\x1b[1;19H中"), true),
            StringComparison.Ordinal);
    }

    [Fact]
    public void RestoresModes()
    {
        const string insert = "abcdef\r\x1b[4h";
        Assert.Contains("\x1b[4h", SuffixOf(Fed(20, 3, insert)), StringComparison.Ordinal);
        AssertResumes(Bytes(insert + "XY"), insert.Length, 20, 3);
        const string lnm = "\x1b[20h\x1b[5Cab";
        Assert.Contains("\x1b[20h", SuffixOf(Fed(20, 3, lnm)), StringComparison.Ordinal);
        AssertResumes(Bytes(lnm + "\ncd"), lnm.Length, 20, 3);
        Assert.Contains(
            "\x1b[?1h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h",
            SuffixOf(Fed(20, 3, "\x1b[?1h\x1b[?2004h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h")),
            StringComparison.Ordinal);
        var alt = Fed(20, 3, "\x1b[?1049hgame");
        Assert.StartsWith("\x1b[?1049h\x1b[H", alt.AnsiScreen(), StringComparison.Ordinal);
        alt.Process(Bytes("\x1b[?1049l"));
        Assert.False(alt.AnsiScreen().StartsWith("\x1b[?", StringComparison.Ordinal));
        Assert.Equal(
            "\x1b[0m" + new string(' ', 20) + "\x1b[0m\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;1H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h",
            SuffixOf(new TerminalEmulator(20, 3, "")));
    }

    [Fact]
    public void RestoresCharsetsWhereTheStreamHonoursThem()
    {
        Assert.Contains(
            "\x1b(0\x1b)0\x0e",
            TerminalState.ScreenStateSuffix(CharsetScreen(20, 3, "\x1b)0\x0eqqq\x1b(0x"), true),
            StringComparison.Ordinal);
        var powerOn = TerminalState.ScreenStateSuffix(CharsetScreen(20, 3, ""), true);
        Assert.DoesNotContain("\x1b(", powerOn, StringComparison.Ordinal);
        foreach (var code in new[] { "B", "0", "U", "V" })
        {
            Assert.Contains(
                $"\x1b({code}\x1b){code}",
                TerminalState.ScreenStateSuffix(CharsetScreen(20, 3, $"\x1b({code}\x1b){code}"), true),
                StringComparison.Ordinal);
        }

        Assert.DoesNotContain(
            "\x1b)",
            TerminalState.ScreenStateSuffix(CharsetScreen(20, 3, "\x1b)B\x0e"), false),
            StringComparison.Ordinal);
        // A designation matching the power-on sets is still sent: curses under
        // TERM=screen designates G1 once and draws every line with SO/SI.
        var designated = CharsetScreen(20, 3, "\x1b(B\x1b)0\x0eqq\x0f");
        Assert.True(designated.CharsetsDesignated);
        Assert.Contains("\x1b(B\x1b)0\x1b[0m", TerminalState.ScreenStateSuffix(designated, true), StringComparison.Ordinal);
        designated.Reset();
        Assert.False(designated.CharsetsDesignated);
        Assert.DoesNotContain("\x1b)", TerminalState.ScreenStateSuffix(designated, true), StringComparison.Ordinal);
        var screen = CharsetScreen(20, 3, "\x1b(U\x1b)V\x0e");
        Assert.Equal(("U", "V", true), screen.CharsetState());
        Assert.True(screen.HasMode(ModeCodes.Decawm));
    }
}
