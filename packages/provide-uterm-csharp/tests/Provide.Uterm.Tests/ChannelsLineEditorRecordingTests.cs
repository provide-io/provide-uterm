//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Channels;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Recording;
using LineEd = Provide.Uterm.LineEditor.LineEditor;
using ReplayEngine = Provide.Uterm.Replay.Replay;

namespace Provide.Uterm.Tests;

public class ChannelsLineEditorRecordingTests
{
    [Fact]
    public void Negotiated_Hello_Seq_ExportRestore()
    {
        var n = Negotiated.Create(new Dictionary<string, int> { ["ctrl"] = 2, ["data"] = 1 }, "ctrl");
        Assert.False(n.IsNegotiated());
        var ack = n.HandleHello(new Hello
        {
            Channels = new Dictionary<string, int> { ["ctrl"] = 3, ["data"] = 1, ["other"] = 9 },
        }, new Dictionary<string, object?> { ["server"] = "uterm" });
        Assert.Equal("hello_ack", ack["type"]?.ToString());
        Assert.True(n.IsNegotiated());
        Assert.True(n.IsNegotiated("ctrl"));
        Assert.Equal(1, n.NextSeq());
        Assert.Equal(2, n.NextSeq("ctrl"));
        Assert.Equal(1, n.NextSeq("data"));

        var grants = n.ExportGrants();
        Assert.Equal(2, grants["ctrl"]); // min(3,2)
        Assert.Equal(1, grants["data"]);

        var n2 = Negotiated.Create(new Dictionary<string, int> { ["ctrl"] = 2, ["data"] = 1 }, "ctrl");
        n2.RestoreGrants(grants.ToDictionary(kv => kv.Key, kv => (object?)kv.Value));
        Assert.True(n2.IsNegotiated("ctrl"));
    }

    [Fact]
    public void Negotiated_Rejects_Invalid()
    {
        Assert.Throws<ArgumentException>(() => Negotiated.Create(new Dictionary<string, int>()));
        Assert.Throws<ArgumentException>(() =>
            Negotiated.Create(new Dictionary<string, int> { [""] = 1 }));
        Assert.Throws<ArgumentException>(() =>
            Negotiated.Create(new Dictionary<string, int> { ["a"] = 1 }, "missing"));

        var n = Negotiated.Create(new Dictionary<string, int> { ["a"] = 1 });
        Assert.Throws<InvalidOperationException>(() => n.IsNegotiated());
        Assert.Throws<ArgumentException>(() =>
            n.HandleHello(new Hello { Channels = new Dictionary<string, int> { ["a"] = 1 } },
                new Dictionary<string, object?> { ["type"] = "x" }));
    }

    [Fact]
    public void ParseChannelHello_FromControlFrame()
    {
        var payload = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = new Dictionary<string, object?> { ["ctrl"] = 1 },
        });
        var hello = Negotiated.ParseChannelHello(payload);
        Assert.NotNull(hello);
        Assert.Equal(1, hello!.Channels["ctrl"]);

        Assert.Null(Negotiated.ParseChannelHello("not a frame"));
        Assert.Null(Negotiated.ParseChannelHello(""));
    }

    [Fact]
    public void LineEditor_Typing_Enter_And_Shortcuts()
    {
        var written = new StringBuilder();
        var ed = new LineEd(maxLength: 8, passwordMode: false, onWrite: s => written.Append(s));
        Assert.False(ed.ProcessChar('h').Done);
        Assert.False(ed.ProcessChar('i').Done);
        Assert.Equal("hi", ed.Buffer());

        // Ctrl+A home, Ctrl+E end, Ctrl+B back, Ctrl+F forward
        Assert.False(ed.ProcessChar('\x01').Done);
        Assert.False(ed.ProcessChar('\x05').Done);
        Assert.False(ed.ProcessChar('\u0002').Done);
        Assert.False(ed.ProcessChar('\x06').Done);

        // Backspace
        Assert.False(ed.ProcessChar('\x7f').Done);

        // Ctrl+U / Ctrl+K / Ctrl+W
        ed.ProcessChar('a');
        ed.ProcessChar(' ');
        ed.ProcessChar('b');
        ed.ProcessChar('\x17');
        ed.ProcessChar('\x15');
        ed.ProcessChar('z');
        ed.ProcessChar('\x0b');

        var (line, done) = ed.ProcessChar('\r');
        Assert.True(done);
        Assert.NotNull(line);
        ed.Reset();
        Assert.Equal("", ed.Buffer());

        // Password mode + max length
        var pw = new LineEd(maxLength: 2, passwordMode: true, onWrite: _ => { });
        pw.ProcessChar('a');
        pw.ProcessChar('b');
        var (l2, d2) = pw.ProcessChar('c'); // over max → bell
        Assert.False(d2);
        Assert.Equal("", l2);
        Assert.Equal("ab", pw.Buffer());
    }

    [Fact]
    public async Task Recording_InMemoryStore_Lifecycle()
    {
        var store = new InMemoryStore();
        await store.StartSessionAsync("s1", new Dictionary<string, object?> { ["host"] = "x" });
        await store.AppendEventsAsync("s1",
        [
            new Event { ["event"] = "read", ["data"] = new Dictionary<string, object?> { ["raw"] = "hi" } },
            new Event { ["event"] = "screen", ["data"] = new Dictionary<string, object?> { ["screen"] = "S" } },
        ]);
        await store.EndSessionAsync("s1");

        var meta = await store.RecordingMetaAsync("s1");
        Assert.True(meta.Exists);
        Assert.True(meta.SizeBytes > 0);

        var all = await store.GetEntriesAsync("s1", new Query { Limit = 0 });
        Assert.True(all.Count >= 3);

        var reads = await store.GetEntriesAsync("s1", new Query { Event = "read", Limit = 10 });
        Assert.All(reads, e => Assert.Equal("read", e["event"]?.ToString()));

        var missing = await store.RecordingMetaAsync("nope");
        Assert.False(missing.Exists);
        Assert.Empty(await store.GetEntriesAsync("nope", new Query()));
        Assert.Equal("", await store.GetPathAsync("s1"));
    }

    [Fact]
    public async Task Recording_NullStore_And_LocalFileStore()
    {
        var nullStore = new NullStore();
        await nullStore.StartSessionAsync("s", new Dictionary<string, object?>());
        await nullStore.AppendEventsAsync("s", Array.Empty<Event>());
        await nullStore.EndSessionAsync("s");
        Assert.False((await nullStore.RecordingMetaAsync("s")).Exists);
        Assert.Empty(await nullStore.GetEntriesAsync("s", new Query()));
        Assert.Equal("", await nullStore.GetPathAsync("s"));

        var dir = Path.Combine(Path.GetTempPath(), "uterm-rec-" + Guid.NewGuid().ToString("N"));
        try
        {
            using var local = new LocalFileStore(dir);
            await local.StartSessionAsync("s1", new Dictionary<string, object?> { ["k"] = "v" });
            await local.AppendEventsAsync("s1",
            [
                new Event { ["event"] = "read", ["data"] = new Dictionary<string, object?> { ["x"] = 1 } },
            ]);
            await local.EndSessionAsync("s1");
            var meta = await local.RecordingMetaAsync("s1");
            Assert.True(meta.Exists);
            var path = await local.GetPathAsync("s1");
            Assert.True(File.Exists(path));
            var entries = await local.GetEntriesAsync("s1", new Query { Limit = 100 });
            Assert.NotEmpty(entries);
        }
        finally
        {
            if (Directory.Exists(dir))
            {
                Directory.Delete(dir, recursive: true);
            }
        }
    }

    [Fact]
    public async Task Replay_RebuildAndReplayLog()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-replay-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var log = Path.Combine(dir, "log.jsonl");
            var b64 = Convert.ToBase64String(Encoding.UTF8.GetBytes("ABC"));
            await File.WriteAllLinesAsync(log,
            [
                """{"event":"write","data":{}}""",
                "{\"event\":\"read\",\"ts\":1.0,\"data\":{\"raw_bytes_b64\":\"" + b64 + "\",\"screen\":\"line1\"}}",
                """{"event":"screen","ts":2.0,"data":{"screen":"line2"}}""",
                "",
            ]);

            var outPath = Path.Combine(dir, "out.bin");
            await ReplayEngine.RebuildRawStreamAsync(log, outPath);
            Assert.Equal("ABC", Encoding.UTF8.GetString(await File.ReadAllBytesAsync(outPath)));

            var sw = new StringWriter();
            var sleeps = new List<TimeSpan>();
            await ReplayEngine.ReplayLogAsync(log, new ReplayEngine.ReplayOptions
            {
                Speed = 100,
                Output = sw,
                Input = new StringReader(""),
                Sleep = t => sleeps.Add(t),
                Events = new[] { "read", "screen" },
            });
            var output = sw.ToString();
            Assert.Contains("line1", output, StringComparison.Ordinal);
            Assert.Contains("line2", output, StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }
}
