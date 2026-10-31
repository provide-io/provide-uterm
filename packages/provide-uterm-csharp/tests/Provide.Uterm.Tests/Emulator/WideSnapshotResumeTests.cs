//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using System.Text.RegularExpressions;
using Provide.Uterm.Render;
using Provide.Uterm.Vt;
using VtScreen = Provide.Uterm.Vt.Screen;

namespace Provide.Uterm.Tests.Emulator;

/// <summary>
/// conformance/snapshot/wide_resume.json holds each stream and the snapshot the
/// Python emulator takes of it in a UTF-8 session. This port writes those bytes,
/// and a screen painted from a snapshot at any point and fed the rest ends where
/// one fed the whole stream does. The C# emulator decodes CP437, so the snapshot
/// is taken here, from the screen a UTF-8 session would hold.
/// </summary>
public class WideSnapshotResumeTests
{
    private const string CorpusRelativePath = "conformance/snapshot/wide_resume.json";

    /// <summary>One terminal token, over characters: a snapshot is never taken inside one.</summary>
    private static readonly Regex Token = new(
        @"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|\P{Cs}|\p{Cs}{2}",
        RegexOptions.Singleline | RegexOptions.CultureInvariant);

    private static readonly JsonElement[] Records = LoadRecords();

    private static JsonElement[] LoadRecords()
    {
        var parts = CorpusRelativePath.Split('/');
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return JsonDocument.Parse(File.ReadAllText(candidate)).RootElement.GetProperty("records").EnumerateArray().ToArray();
            }
        }

        throw new FileNotFoundException("wide resume corpus not found above " + AppContext.BaseDirectory, CorpusRelativePath);
    }

    public static TheoryData<int> RecordIndexes()
    {
        var data = new TheoryData<int>();
        for (var i = 0; i < Records.Length; i++)
        {
            data.Add(i);
        }

        return data;
    }

    /// <summary>A screen fed as a UTF-8 session's is: designations and SO/SI honoured.</summary>
    private static (VtScreen Screen, VtStream Stream) Utf8Session(int cols, int rows, string stream)
    {
        var screen = new VtScreen(cols, rows);
        var vtStream = new VtStream(screen) { UseUtf8 = false };
        vtStream.Feed(stream);
        return (screen, vtStream);
    }

    private static string SnapshotOf(VtScreen screen) =>
        TerminalState.ScreenStatePrefix(screen) +
        string.Join("\n", RenderBuffer.RenderScreenLines(screen, screen.Columns, screen.Lines)) +
        TerminalState.ScreenStateSuffix(screen, true);

    private static (string Display, int X, int Y, string Ansi) StateOf(VtScreen screen) =>
        (string.Join("\n", screen.Display()), screen.Cursor.X, screen.Cursor.Y, SnapshotOf(screen));

    [Fact]
    public void HasTheRecordsEveryPortReplays() => Assert.True(Records.Length > 15);

    [Theory]
    [MemberData(nameof(RecordIndexes))]
    public void WritesTheRecordedSnapshot(int index)
    {
        var record = Records[index];
        var (screen, _) = Utf8Session(record.GetProperty("cols").GetInt32(), record.GetProperty("rows").GetInt32(), record.GetProperty("stream").GetString()!);
        Assert.Equal(record.GetProperty("ansi_screen").GetString(), SnapshotOf(screen));
    }

    [Theory]
    [MemberData(nameof(RecordIndexes))]
    public void ResumesExactlyFromASnapshotAtEveryPoint(int index)
    {
        var record = Records[index];
        var cols = record.GetProperty("cols").GetInt32();
        var rows = record.GetProperty("rows").GetInt32();
        var stream = record.GetProperty("stream").GetString()!;
        var expected = StateOf(Utf8Session(cols, rows, stream).Screen);
        foreach (var split in new[] { 0 }.Concat(Token.Matches(stream).Select(m => m.Index + m.Length)))
        {
            var (live, _) = Utf8Session(cols, rows, stream[..split]);
            var (viewer, viewerStream) = Utf8Session(cols, rows, SnapshotOf(live).Replace("\n", "\r\n", StringComparison.Ordinal));
            viewerStream.Feed(stream[split..]);
            Assert.True(StateOf(viewer) == expected, $"{record.GetProperty("name").GetString()}: split at {split}");
        }
    }

    [Fact]
    public void DrawsAWideCharacterOnceWithNothingForItsSecondCell()
    {
        var (screen, _) = Utf8Session(6, 1, "中a");
        Assert.Equal(new[] { "\x1b[0m中a   \x1b[0m" }, RenderBuffer.RenderScreenLines(screen, 6, 1));
    }

    [Fact]
    public void LeavesBlankALostFirstHalfWithNoCellLeftOfIt()
    {
        // Two second halves with their first halves gone, in the last two
        // columns (an insert shifted the first of them there): the first is
        // recreated, and the second, with no cell left of it, left blank.
        var (screen, _) = Utf8Session(4, 1, "a中\x1b[1;2Hb\x1b[1;3H\x1b[@\x1b[1;2H中\x1b[1;2Hc");
        Assert.Equal(new[] { "\x1b[0mac　\x1b[3G\x1b[P\x1b[C\x1b[0m" }, RenderBuffer.RenderScreenLines(screen, 4, 1));
        var (one, _) = Utf8Session(2, 1, "中\x1b[1;1H\x1b[P");
        Assert.Equal(new[] { "\x1b[0m" }, RenderBuffer.RenderScreenLines(one, 1, 1));
    }

    [Fact]
    public void DrawsAnEmptyCellAsABlankWhenTheCursorIsDeferredOverIt()
    {
        // A resize can leave the cursor deferred past the last column over two
        // empty second halves; the reference draws an empty cell as a blank.
        var (screen, _) = Utf8Session(4, 1, "\u4e2d\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H\x1b[P\x1b[1;3H");
        screen.Resize(1, 2);
        Assert.Contains("\x1b[1;1H\x1b[0m \x1b[20l", SnapshotOf(screen), StringComparison.Ordinal);
    }

    [Fact]
    public void DrawsAnEmptyCellAsABlankOnAOneColumnScreen()
    {
        // An empty second half with no cell left of it and no neighbouring row.
        var (screen, _) = Utf8Session(2, 1, "\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H");
        screen.Resize(1, 1);
        Assert.Contains("\x1b[1;1H\x1b[0m \x1b[20l", SnapshotOf(screen), StringComparison.Ordinal);
    }
}
