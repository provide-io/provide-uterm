//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Library-level session recording demo (C#) — screen snapshots → JSONL.

using Provide.Uterm.Recording;

const string magenta = "\u001b[1;35m";
const string green = "\u001b[1;32m";
const string cyan = "\u001b[1;36m";
const string dim = "\u001b[2m";
const string reset = "\u001b[0m";
const string bold = "\u001b[1m";

static void Banner(string title)
{
    var bar = new string('═', title.Length + 4);
    Console.WriteLine();
    Console.WriteLine($"{magenta}{bar}{reset}");
    Console.WriteLine($"{magenta}  {bold}{title}{reset}{magenta}  {reset}");
    Console.WriteLine($"{magenta}{bar}{reset}");
    Console.WriteLine();
}

static void Info(string msg) => Console.WriteLine($"{cyan}  → {msg}{reset}");
static void Ok(string msg) => Console.WriteLine($"{green}  ✓ {msg}{reset}");
static void Kv(string key, object? value) =>
    Console.WriteLine($"    {dim}{key}:{reset} {bold}{value}{reset}");

Banner("provide-uterm recording — C#");
Info("language=csharp  store=LocalFileStore");

var tmp = Path.Combine(Path.GetTempPath(), "uterm-rec-cs-" + Guid.NewGuid().ToString("N")[..8]);
Directory.CreateDirectory(tmp);
try
{
    using var store = new LocalFileStore(tmp);
    const string sid = "demo-recording-cs";
    await store.StartSessionAsync(
        sid,
        new Dictionary<string, object?>
        {
            ["lang"] = "csharp",
            ["feature"] = "session_recording",
            ["demo"] = "recording_matrix",
        });
    Ok($"session started: {sid}");

    string[] screens =
    [
        "",
        "=== provide-uterm: session recording active ===\n",
        "=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n",
        "=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n[deploy] step 2: running migrations\n",
        "=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n[deploy] step 2: running migrations\n[deploy] step 3: restarting services\n",
        "=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n[deploy] step 2: running migrations\n[deploy] step 3: restarting services\n[deploy] healthcheck: ok — recording complete\n",
    ];

    for (var i = 0; i < screens.Length; i++)
    {
        var screen = screens[i];
        var ev = new Event
        {
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["event"] = "snapshot",
            ["session_id"] = sid,
            ["data"] = new Dictionary<string, object?>
            {
                ["seq"] = i,
                ["screen"] = screen,
                ["cols"] = 80,
                ["rows"] = 24,
                ["source"] = "csharp",
            },
        };
        await store.AppendEventsAsync(sid, [ev]);
        Info($"snapshot {i}: {screen.Length} screen bytes");
        await Task.Delay(150);
    }

    await store.EndSessionAsync(sid);
    var meta = await store.RecordingMetaAsync(sid);
    var path = await store.GetPathAsync(sid);
    var entries = await store.GetEntriesAsync(sid, new Query { Limit = 50 });
    Kv("exists", meta.Exists);
    Kv("size_bytes", meta.SizeBytes);
    Kv("path", path);
    Kv("entries", entries.Count);
    Kv("snapshots", entries.Count(e => e.TryGetValue("event", out var ev) && $"{ev}" == "snapshot"));
    if (!string.IsNullOrEmpty(path) && File.Exists(path))
    {
        foreach (var line in File.ReadLines(path).Take(2))
        {
            var s = line.Length > 100 ? line[..100] + "…" : line;
            Info("jsonl: " + s);
        }
    }

    Ok("C# LocalFileStore: screen snapshots persisted as JSONL");
}
finally
{
    try
    {
        Directory.Delete(tmp, recursive: true);
    }
    catch
    {
        // ignore
    }
}
