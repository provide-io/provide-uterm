//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;
using Provide.Uterm.Annotation;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// Read-path annotation: snapshots read as text, one record per match, and
/// escape sequences split across chunks. Parity with the reference's
/// <c>HostedSessionRuntime._log_snapshot</c> / <c>_scan_output</c> and
/// <c>provide-uterm-server/tests/server/test_read_path_dedupe.py</c>, pinned by
/// <c>testdata/read_path_golden.json</c> (generated from the reference by
/// <c>testdata/gen_read_path_golden.py</c>).
/// </summary>
public sealed class SessionAnnotatorReadPathTests
{
    private const string Sudo = "sudo command detected: sudo";
    private const string Rm = "Recursive force-remove detected: rm -rf";

    private static readonly JsonNode Golden =
        JsonNode.Parse(File.ReadAllText(TestData.PathTo("read_path_golden.json")))!;

    private sealed class Harness
    {
        public bool Recording = true;
        public readonly List<Dictionary<string, object?>> Recorded = [];
        public readonly SessionAnnotator Annotator;

        public Harness()
        {
            Annotator = new SessionAnnotator(new PatternDetector(), () => Recording, data =>
            {
                Recorded.Add(data);
                return Task.CompletedTask;
            });
        }

        public Task Term(string data) => Annotator.RecordAsync(Annotator.ScanOutput(data));

        public Task Snapshot(string screen) => Annotator.RecordAsync(
            Annotator.ScanSnapshot(new Dictionary<string, object?> { ["screen"] = screen }));

        public List<string> Descriptions => Recorded.Select(d => (string)d["description"]!).ToList();
    }

    // ------------------------------------------------------------------
    // Golden corpus from the reference
    // ------------------------------------------------------------------

    [Fact]
    public void ConstantsMatchTheReference()
    {
        Assert.Equal(64, SessionAnnotator.MaxEscapeCarry);
        Assert.Equal(1024, SessionAnnotator.MaxReadAnnotationKeys);
        Assert.Equal(SessionAnnotator.MaxEscapeCarry, (int)Golden["max_escape_carry"]!);
        Assert.Equal(SessionAnnotator.MaxReadAnnotationKeys, (int)Golden["max_read_annotation_keys"]!);
    }

    [Fact]
    public void SplitIncompleteEscapeMatchesTheReference()
    {
        var cases = Golden["split"]!.AsArray();
        Assert.NotEmpty(cases);
        foreach (var c in cases)
        {
            var (complete, carry) = SessionAnnotator.SplitIncompleteEscape((string)c!["input"]!);
            Assert.Equal((string)c["complete"]!, complete);
            Assert.Equal((string)c["carry"]!, carry);
        }
    }

    [Fact]
    public async Task ScenariosMatchTheReference()
    {
        var scenarios = Golden["scenarios"]!.AsObject();
        Assert.NotEmpty(scenarios);
        foreach (var (name, scenario) in scenarios)
        {
            var h = new Harness();
            foreach (var step in scenario!["steps"]!.AsArray())
            {
                var kind = (string)step![0]!;
                var text = (string)step[1]!;
                await (kind == "term" ? h.Term(text) : h.Snapshot(text));
            }

            var want = scenario["annotations"]!.AsArray()
                .Select(a => $"{(string)a!["label"]!}|{(string)a["description"]!}").ToList();
            var got = h.Recorded.Select(d => $"{d["label"]}|{d["description"]}").ToList();
            Assert.True(want.SequenceEqual(got), $"{name}: want [{string.Join(", ", want)}] got [{string.Join(", ", got)}]");
            Assert.Equal((string)scenario["carry"]!, h.Annotator.EscapeCarry);
        }
    }

    // ------------------------------------------------------------------
    // Finding 2: snapshots are read as text
    // ------------------------------------------------------------------

    [Fact]
    public async Task AStyledSnapshotIsReadThroughItsEscapeSequences()
    {
        // A rendered screen styles runs and resets at row ends: "su" + reset +
        // "do" is "sudo" on the screen, and only on the screen.
        var h = new Harness();

        await h.Snapshot("\x1b[1m$ \x1b[31msu\x1b[0mdo\x1b[0m ls\x1b[0m\n");

        Assert.Equal([Sudo], h.Descriptions);
    }

    // ------------------------------------------------------------------
    // Finding 9: escape sequences split across chunks
    // ------------------------------------------------------------------

    [Fact]
    public async Task AnEscapeSplitAcrossChunksDoesNotHideAMatch()
    {
        var h = new Harness();

        await h.Term("$ \x1b[1");
        Assert.Equal("\x1b[1", h.Annotator.EscapeCarry);
        await h.Term("msudo rm -rf /\r\n");

        Assert.Equal([Rm, Sudo], h.Descriptions.Order().ToList());
        Assert.Equal("", h.Annotator.EscapeCarry);
    }

    [Fact]
    public async Task AnOverlongUnterminatedSequenceIsReleased()
    {
        var h = new Harness();

        await h.Term("\x1b[" + string.Concat(Enumerable.Repeat("1;", SessionAnnotator.MaxEscapeCarry)));

        Assert.Equal("", h.Annotator.EscapeCarry);
    }

    [Fact]
    public async Task AnEmptyChunkLeavesTheCarryAlone()
    {
        var h = new Harness();
        await h.Term("$ \x1b[1");

        await h.Term("");

        Assert.Equal("\x1b[1", h.Annotator.EscapeCarry);
    }

    // ------------------------------------------------------------------
    // Finding 8: stream + snapshot dedupe
    // ------------------------------------------------------------------

    [Fact]
    public async Task ASnapshotAfterTheStreamDoesNotRepeatItsMatch()
    {
        var h = new Harness();

        await h.Term("$ sudo ls\r\n");
        await h.Snapshot("$ sudo ls\n");

        Assert.Equal([Sudo], h.Descriptions);
    }

    [Fact]
    public async Task RepeatedIdenticalSnapshotsAnnotateOnce()
    {
        var h = new Harness();

        for (var i = 0; i < 3; i++) await h.Snapshot("$ rm -rf build\n");

        Assert.Equal([Rm], h.Descriptions);
    }

    [Fact]
    public async Task ACommandStreamedTwiceIsAnnotatedTwice()
    {
        var h = new Harness();

        await h.Term("$ sudo ls\r\n");
        await h.Term("$ sudo ls\r\n");
        await h.Snapshot("$ sudo ls\n$ sudo ls\n");

        Assert.Equal([Sudo, Sudo], h.Descriptions);
    }

    [Fact]
    public async Task ASnapshotStillRecordsWhatTheStreamNeverSaw()
    {
        var h = new Harness();

        await h.Term("$ sudo ls\r\n");
        await h.Snapshot("$ sudo ls\n$ rm -rf build\n");

        Assert.Equal([Sudo, Rm], h.Descriptions);
    }

    [Fact]
    public void TheKeyIsTheRuleLabelAndItsDescribedMatch()
    {
        var annotation = new Provide.Uterm.Annotation.Annotation
        {
            Label = "privilege_escalation",
            Description = "sudo command detected: sudo",
        };

        Assert.Equal("privilege_escalation\0sudo command detected: sudo", SessionAnnotator.ReadAnnotationKey(annotation));
    }

    [Fact]
    public async Task TheKeySetIsBoundedAndStartsOverWhenFull()
    {
        var h = new Harness();
        for (var i = 0; i < SessionAnnotator.MaxReadAnnotationKeys; i++)
        {
            await h.Term($"$ ssh u{i}@host\r\n");
        }

        Assert.Equal(SessionAnnotator.MaxReadAnnotationKeys, h.Annotator.ReadAnnotationKeyCount);

        await h.Term("$ ssh next@host\r\n");

        Assert.Equal(1, h.Annotator.ReadAnnotationKeyCount);
        // The set started over with only the newest key: an earlier match is
        // no longer remembered, so a snapshot of it records it again.
        await h.Snapshot("$ ssh u0@host\n");
        Assert.Equal("SSH connection detected: ssh u0@", h.Descriptions[^1]);
        await h.Snapshot("$ ssh next@host\n");
        Assert.Equal("SSH connection detected: ssh u0@", h.Descriptions[^1]);
    }

    [Fact]
    public async Task ANewRecordingStartsWithNoKeysAndNoCarry()
    {
        var h = new Harness();
        await h.Term("$ sudo ls\r\n\x1b[1");
        Assert.Equal(1, h.Annotator.ReadAnnotationKeyCount);
        Assert.Equal("\x1b[1", h.Annotator.EscapeCarry);

        // The recording ends: the next frame sees it, and the state goes with it.
        h.Recording = false;
        await h.Term("anything");
        Assert.Equal(0, h.Annotator.ReadAnnotationKeyCount);
        Assert.Equal("", h.Annotator.EscapeCarry);

        // A new recording annotates what it sees afresh.
        h.Recording = true;
        await h.Snapshot("$ sudo ls\n");
        Assert.Equal([Sudo, Sudo], h.Descriptions);
    }

    [Theory]
    [InlineData("snapshot")]
    [InlineData("input")]
    public void AnyScanThatFindsTheRecordingEndedClearsItsState(string scan)
    {
        var h = new Harness();
        h.Annotator.ScanOutput("$ sudo ls\r\n\x1b[1");
        h.Recording = false;

        if (scan == "snapshot") h.Annotator.ScanSnapshot(new Dictionary<string, object?> { ["screen"] = "x" });
        else h.Annotator.ScanInput("x");

        Assert.Equal(0, h.Annotator.ReadAnnotationKeyCount);
        Assert.Equal("", h.Annotator.EscapeCarry);
    }
}
