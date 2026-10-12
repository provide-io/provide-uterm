//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Recording;
using Provide.Uterm.SessionLogger;

namespace Provide.Uterm.Tests;

/// <summary>
/// A recording never carries one session instance's entries into another's.
/// A session id can be reused (a PAM capture session is named after a pid, and
/// pids are reused), so StartSessionAsync continues the recording on file only
/// when the same instance started it, and otherwise begins afresh. Port of
/// test_recording_instances.py.
/// </summary>
public sealed class RecordingInstanceTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "uterm-rec-instance-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        if (Directory.Exists(_dir))
        {
            Directory.Delete(_dir, recursive: true);
        }
    }

    private IRecordingStore Make(string kind) => kind == "file" ? new LocalFileStore(_dir) : new InMemoryStore();

    private static Dictionary<string, object?> Meta(string? instance) =>
        instance is null ? new() : new() { ["instance"] = instance };

    private static async Task RecordOne(IRecordingStore store, string sid, string? instance, string text)
    {
        await store.StartSessionAsync(sid, Meta(instance));
        await store.AppendEventsAsync(sid, [new Event
        {
            ["ts"] = 1.0, ["event"] = "read", ["data"] = new Dictionary<string, object?> { ["text"] = text },
        }]);
        await store.EndSessionAsync(sid);
    }

    private static async Task<List<string>> Texts(IRecordingStore store, string sid, string eventName = "read", string field = "text")
    {
        var entries = await store.GetEntriesAsync(sid, new Query { Limit = 500, Event = eventName });
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(entries));
        return doc.RootElement.EnumerateArray()
            .Select(e => e.GetProperty("data").GetProperty(field).GetString()!)
            .ToList();
    }

    [Theory]
    [InlineData("file")]
    [InlineData("memory")]
    public async Task NewInstanceUnderReusedIdSeesNoneOfTheOldEntries(string kind)
    {
        var store = Make(kind);
        await RecordOne(store, "pam-u-capture-7", "first", "alice-secret");
        await RecordOne(store, "pam-u-capture-7", "second", "bob-output");
        Assert.Equal(["bob-output"], await Texts(store, "pam-u-capture-7"));
    }

    [Theory]
    [InlineData("file")]
    [InlineData("memory")]
    public async Task StartWithoutInstanceAlwaysBeginsAfresh(string kind)
    {
        var store = Make(kind);
        await RecordOne(store, "s", null, "one");
        await RecordOne(store, "s", null, "two");
        Assert.Equal(["two"], await Texts(store, "s"));
    }

    [Theory]
    [InlineData("file")]
    [InlineData("memory")]
    public async Task SameInstanceReconnectingKeepsOneRecording(string kind)
    {
        var store = Make(kind);
        await RecordOne(store, "s", "i1", "first-connection");
        await RecordOne(store, "s", "i1", "second-connection");
        Assert.Equal(["first-connection", "second-connection"], await Texts(store, "s"));
    }

    [Fact]
    public async Task OldFileIsKeptAsideNotServed()
    {
        var store = new LocalFileStore(_dir);
        await RecordOne(store, "s", "first", "old");
        await RecordOne(store, "s", "second", "new");
        var retired = Directory.GetFiles(_dir, "s.*.jsonl");
        Assert.Single(retired);
        Assert.Contains("old", File.ReadAllText(retired[0]));
        Assert.DoesNotContain("old", File.ReadAllText(Path.Combine(_dir, "s.jsonl")));
        if (!OperatingSystem.IsWindows())
        {
            Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, File.GetUnixFileMode(retired[0]));
        }
    }

    [Fact]
    public async Task UnreadableRecordingIsRetiredNotAppendedTo()
    {
        Directory.CreateDirectory(_dir);
        await File.WriteAllTextAsync(Path.Combine(_dir, "s.jsonl"), "not json\n");
        var store = new LocalFileStore(_dir);
        await RecordOne(store, "s", "i1", "mine");
        Assert.DoesNotContain("not json", File.ReadAllText(Path.Combine(_dir, "s.jsonl")));
    }

    [Fact]
    public async Task HandleLeftOpenByUnendedInstanceIsNotReused()
    {
        var store = new LocalFileStore(_dir);
        await store.StartSessionAsync("s", Meta("first"));
        await store.AppendEventsAsync("s", [new Event
        {
            ["ts"] = 1.0, ["event"] = "read", ["data"] = new Dictionary<string, object?> { ["text"] = "old" },
        }]);
        await RecordOne(store, "s", "second", "new");
        Assert.Equal(["new"], await Texts(store, "s"));
    }

    [Fact]
    public async Task SessionLoggerPassesItsInstanceSoReconnectsShareOneRecording()
    {
        var store = new InMemoryStore();
        async Task OneConnection(string? instance, string keys)
        {
            var logger = new SessionLogger.SessionLogger(store, new SessionLoggerOptions { Instance = instance });
            await logger.StartAsync("s");
            await logger.LogEventAsync("send", new Dictionary<string, object?> { ["keys"] = keys });
            await logger.StopAsync();
        }

        await OneConnection("i1", "alice-1");
        await OneConnection("i1", "alice-2");
        Assert.Equal(["alice-1", "alice-2"], await Texts(store, "s", "send", "keys"));
        await OneConnection("i2", "bob");
        Assert.Equal(["bob"], await Texts(store, "s", "send", "keys"));
    }
}
