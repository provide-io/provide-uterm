//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.FileIo;

namespace Provide.Uterm.Recording;

/// <summary>One JSON-serializable recording entry.</summary>
public sealed class Event : Dictionary<string, object?>
{
    public Event()
    {
    }

    public Event(IDictionary<string, object?> source) : base(source)
    {
    }
}

public sealed class Meta
{
    public string SessionId { get; set; } = "";
    public bool Exists { get; set; }
    public long SizeBytes { get; set; }
    public string Path { get; set; } = "";
}

public sealed class Query
{
    public int Limit { get; set; }
    public int? Offset { get; set; }
    public string Event { get; set; } = "";
}

/// <summary>Interface for persisting and retrieving session recordings.</summary>
public interface IRecordingStore
{
    Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata);
    Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events);
    Task EndSessionAsync(string sessionId);
    Task<Meta> RecordingMetaAsync(string sessionId);
    Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query);
    Task<string> GetPathAsync(string sessionId);
}

internal static class RecordingHelpers
{
    public static int NormalizeLimit(int limit)
    {
        if (limit == 0)
        {
            limit = 200;
        }

        return Math.Max(1, Math.Min(limit, 500));
    }

    public static Event LifecycleEvent(string name, string sessionId, IReadOnlyDictionary<string, object?>? data)
    {
        data ??= new Dictionary<string, object?>();
        return new Event
        {
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["event"] = name,
            ["data"] = data is Dictionary<string, object?> d ? d : new Dictionary<string, object?>(data),
            ["session_id"] = sessionId,
        };
    }
}

/// <summary>Ephemeral in-memory store.</summary>
public sealed class InMemoryStore : IRecordingStore
{
    private readonly object _lock = new();
    private readonly Dictionary<string, List<Event>> _sessions = new();

    public Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata)
    {
        lock (_lock)
        {
            _sessions[sessionId] = [RecordingHelpers.LifecycleEvent("log_start", sessionId, metadata)];
        }

        return Task.CompletedTask;
    }

    public Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events)
    {
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sessionId, out var list))
            {
                list = [];
                _sessions[sessionId] = list;
            }

            list.AddRange(events);
        }

        return Task.CompletedTask;
    }

    public Task EndSessionAsync(string sessionId)
    {
        lock (_lock)
        {
            if (_sessions.TryGetValue(sessionId, out var list))
            {
                list.Add(RecordingHelpers.LifecycleEvent("log_stop", sessionId, null));
            }
        }

        return Task.CompletedTask;
    }

    public Task<Meta> RecordingMetaAsync(string sessionId)
    {
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sessionId, out var list))
            {
                return Task.FromResult(new Meta { SessionId = sessionId, Exists = false });
            }

            var size = list.Sum(e => JsonSerializer.Serialize(e).Length + 1);
            return Task.FromResult(new Meta
            {
                SessionId = sessionId,
                Exists = true,
                SizeBytes = size,
            });
        }
    }

    public Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query)
    {
        lock (_lock)
        {
            if (!_sessions.TryGetValue(sessionId, out var list))
            {
                return Task.FromResult<IReadOnlyList<Event>>(Array.Empty<Event>());
            }

            IEnumerable<Event> filtered = list;
            if (!string.IsNullOrEmpty(query.Event))
            {
                filtered = filtered.Where(e =>
                    e.TryGetValue("event", out var ev) && ev as string == query.Event);
            }

            var limit = RecordingHelpers.NormalizeLimit(query.Limit);
            var arr = filtered.ToList();
            if (query.Offset is int offset)
            {
                // Negative offset skips nothing (Python/Go parity).
                arr = arr.Skip(Math.Max(0, offset)).Take(limit).ToList();
            }
            else
            {
                arr = arr.TakeLast(limit).ToList();
            }

            return Task.FromResult<IReadOnlyList<Event>>(arr);
        }
    }

    public Task<string> GetPathAsync(string sessionId) => Task.FromResult("");
}

/// <summary>No-op store.</summary>
public sealed class NullStore : IRecordingStore
{
    public Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata) =>
        Task.CompletedTask;

    public Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events) => Task.CompletedTask;

    public Task EndSessionAsync(string sessionId) => Task.CompletedTask;

    public Task<Meta> RecordingMetaAsync(string sessionId) =>
        Task.FromResult(new Meta { SessionId = sessionId, Exists = false });

    public Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query) =>
        Task.FromResult<IReadOnlyList<Event>>(Array.Empty<Event>());

    public Task<string> GetPathAsync(string sessionId) => Task.FromResult("");
}

/// <summary>File-backed store using one JSONL file per session.</summary>
public sealed class LocalFileStore : IRecordingStore, IDisposable
{
    private readonly string _directory;
    private readonly object _lock = new();
    private readonly Dictionary<string, FileStream> _files = new();

    public LocalFileStore(string directory) => _directory = directory;

    private string PathFor(string sessionId) => System.IO.Path.Combine(_directory, sessionId + ".jsonl");

    private static async Task WriteEventsAsync(FileStream fs, IReadOnlyList<Event> events)
    {
        foreach (var evt in events)
        {
            var line = JsonSerializer.Serialize(evt) + "\n";
            var bytes = Encoding.UTF8.GetBytes(line);
            await fs.WriteAsync(bytes);
        }

        await fs.FlushAsync();
    }

    public async Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata)
    {
        FileStream fs;
        lock (_lock)
        {
            fs = FileIo.FileIo.SecureOpenAppend(PathFor(sessionId));
            _files[sessionId] = fs;
        }

        await WriteEventsAsync(fs, [RecordingHelpers.LifecycleEvent("log_start", sessionId, metadata)]);
    }

    public async Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events)
    {
        FileStream fs;
        lock (_lock)
        {
            if (!_files.TryGetValue(sessionId, out fs!))
            {
                fs = FileIo.FileIo.SecureOpenAppend(PathFor(sessionId));
                _files[sessionId] = fs;
            }
        }

        await WriteEventsAsync(fs, events);
    }

    public async Task EndSessionAsync(string sessionId)
    {
        FileStream? fs;
        lock (_lock)
        {
            if (!_files.Remove(sessionId, out fs))
            {
                return;
            }
        }

        await WriteEventsAsync(fs, [RecordingHelpers.LifecycleEvent("log_stop", sessionId, null)]);
        await fs.DisposeAsync();
    }

    public Task<Meta> RecordingMetaAsync(string sessionId)
    {
        var path = PathFor(sessionId);
        if (!File.Exists(path))
        {
            // Match Python: no file → exists false and no path string.
            return Task.FromResult(new Meta { SessionId = sessionId, Exists = false });
        }

        var info = new FileInfo(path);
        return Task.FromResult(new Meta
        {
            SessionId = sessionId,
            Exists = true,
            SizeBytes = info.Length,
            Path = path,
        });
    }

    public async Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query)
    {
        var path = PathFor(sessionId);
        if (!File.Exists(path))
        {
            return Array.Empty<Event>();
        }

        var all = new List<Event>();
        // FileShare.ReadWrite: writers keep the append handle open for the session
        // lifetime; Windows exclusive locks reject File.ReadLinesAsync (share=None).
        await using (var fs = new FileStream(
                         path,
                         FileMode.Open,
                         FileAccess.Read,
                         FileShare.ReadWrite | FileShare.Delete,
                         bufferSize: 4096,
                         useAsync: true))
        using (var reader = new StreamReader(fs, Encoding.UTF8))
        {
            string? line;
            while ((line = await reader.ReadLineAsync().ConfigureAwait(false)) is not null)
            {
                if (string.IsNullOrWhiteSpace(line))
                {
                    continue;
                }

                var dict = JsonSerializer.Deserialize<Dictionary<string, object?>>(line);
                if (dict is null)
                {
                    continue;
                }

                all.Add(new Event(dict));
            }
        }

        IEnumerable<Event> filtered = all;
        if (!string.IsNullOrEmpty(query.Event))
        {
            filtered = filtered.Where(e =>
                e.TryGetValue("event", out var ev) && ev?.ToString() == query.Event);
        }

        var limit = RecordingHelpers.NormalizeLimit(query.Limit);
        var arr = filtered.ToList();
        if (query.Offset is int offset)
        {
            // Negative offset skips nothing (Python/Go parity).
            return arr.Skip(Math.Max(0, offset)).Take(limit).ToList();
        }

        return arr.TakeLast(limit).ToList();
    }

    public Task<string> GetPathAsync(string sessionId)
    {
        var path = PathFor(sessionId);
        // Match Python get_path: only return a path when the file exists.
        return Task.FromResult(File.Exists(path) ? path : "");
    }

    public void Dispose()
    {
        lock (_lock)
        {
            foreach (var fs in _files.Values)
            {
                fs.Dispose();
            }

            _files.Clear();
        }
    }
}
