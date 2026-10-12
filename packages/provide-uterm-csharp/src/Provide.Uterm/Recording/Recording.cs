//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
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

/// <summary>
/// Interface for persisting and retrieving session recordings.
/// A session id can be reused by a later, unrelated session, so a store must
/// never let one session instance read another's entries: StartSessionAsync
/// continues the recording held for the id only when metadata["instance"]
/// (<see cref="RecordingInstance.Key"/>) names the instance that started it (a
/// reconnect), and otherwise — a different instance, or none named — begins
/// afresh, keeping or dropping the old recording but never serving it under
/// the id again.
/// </summary>
public interface IRecordingStore
{
    Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata);
    Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events);
    Task EndSessionAsync(string sessionId);
    Task<Meta> RecordingMetaAsync(string sessionId);
    Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query);
    Task<string> GetPathAsync(string sessionId);

    /// <summary>
    /// Retire the session's recording: afterwards nothing is served for the id
    /// until a new recording starts. Called when a session is deleted (and
    /// before one is created), so the id's next holder is never served this
    /// one's entries. A file store moves the recording aside, an in-memory store
    /// drops it. The default does nothing, so a store written before this
    /// existed still compiles and keeps only the StartSessionAsync instance rule.
    /// </summary>
    Task RetireSessionAsync(string sessionId) => Task.CompletedTask;
}

/// <summary>
/// Names the session instance a recording belongs to. Port of the reference's
/// RECORDING_INSTANCE_KEY.
/// </summary>
public static class RecordingInstance
{
    /// <summary>The StartSessionAsync metadata key naming the instance.</summary>
    public const string Key = "instance";

    // How much of a recording's first line is read to find its instance.
    private const int FirstLineLimit = 64 * 1024;

    /// <summary>The instance StartSessionAsync metadata names, or null for none.</summary>
    public static string? Of(IReadOnlyDictionary<string, object?>? metadata)
    {
        if (metadata is null || !metadata.TryGetValue(Key, out var value))
        {
            return null;
        }

        var text = value switch
        {
            string str => str,
            JsonElement { ValueKind: JsonValueKind.String } el => el.GetString(),
            _ => null,
        };
        return string.IsNullOrEmpty(text) ? null : text;
    }

    /// <summary>
    /// A unique name to move a superseded recording aside to. It keeps the
    /// .jsonl suffix, so the retention sweep still ages it out, and puts a "."
    /// before it that no session id may contain, so no session's recording
    /// path can ever name it. Port of retired_recording_name.
    /// </summary>
    public static string RetiredName(string sessionId)
    {
        var ns = (DateTimeOffset.UtcNow.UtcTicks - DateTimeOffset.UnixEpoch.UtcTicks) * 100;
        return $"{sessionId}.{ns}-{Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(4))}.jsonl";
    }

    // The instance that started the recording at path, or null when it cannot
    // say. A symlink planted at the path is not a recording this store wrote.
    private static string? OnFile(string path)
    {
        try
        {
            var info = new FileInfo(path);
            if (info.LinkTarget is not null || (info.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                return null;
            }

            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var buffer = new byte[FirstLineLimit];
            var length = fs.Read(buffer, 0, buffer.Length);
            var end = Array.IndexOf(buffer, (byte)'\n', 0, length);
            using var doc = JsonDocument.Parse(buffer.AsMemory(0, end < 0 ? length : end));
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("event", out var ev) || ev.ValueKind != JsonValueKind.String
                || ev.GetString() != "log_start"
                || !root.TryGetProperty("data", out var data) || data.ValueKind != JsonValueKind.Object
                || !data.TryGetProperty(Key, out var inst) || inst.ValueKind != JsonValueKind.String)
            {
                return null;
            }

            var text = inst.GetString();
            return string.IsNullOrEmpty(text) ? null : text;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException)
        {
            return null;
        }
    }

    /// <summary>Move the recording at path aside unless instance started it.</summary>
    internal static void RetireForeign(string path, string sessionId, string? instance)
    {
        var info = new FileInfo(path);
        if (!info.Exists && info.LinkTarget is null)
        {
            return;
        }

        if (instance is not null && OnFile(path) == instance)
        {
            return;
        }

        File.Move(path, System.IO.Path.Combine(System.IO.Path.GetDirectoryName(path) ?? ".", RetiredName(sessionId)));
    }
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
    private readonly Dictionary<string, string?> _instances = new();

    /// <summary>
    /// Record log_start. A recording another instance started is dropped first
    /// (see <see cref="IRecordingStore"/>); the same instance continues it.
    /// </summary>
    public Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata)
    {
        lock (_lock)
        {
            var instance = RecordingInstance.Of(metadata);
            var start = RecordingHelpers.LifecycleEvent("log_start", sessionId, metadata);
            if (instance is not null
                && _instances.TryGetValue(sessionId, out var previous) && previous == instance
                && _sessions.TryGetValue(sessionId, out var list))
            {
                list.Add(start);
            }
            else
            {
                _sessions[sessionId] = [start];
            }

            _instances[sessionId] = instance;
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

    /// <summary>Drop the session's recording: no later holder of the id is served it.</summary>
    public Task RetireSessionAsync(string sessionId)
    {
        lock (_lock)
        {
            _sessions.Remove(sessionId);
            _instances.Remove(sessionId);
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

    /// <summary>
    /// Open the session file and write log_start. A handle an earlier start
    /// left open is closed first, and a recording on file that another instance
    /// started is moved aside (see <see cref="IRecordingStore"/>): whether this
    /// start continues it is decided from the file alone.
    /// </summary>
    public async Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata)
    {
        FileStream fs;
        lock (_lock)
        {
            if (_files.Remove(sessionId, out var stale))
            {
                stale.Dispose();
            }

            var path = PathFor(sessionId);
            RecordingInstance.RetireForeign(path, sessionId, RecordingInstance.Of(metadata));
            fs = FileIo.FileIo.SecureOpenAppend(path);
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

    /// <summary>
    /// Move the session's recording aside: kept for retention, served to nobody.
    /// A handle still open for it is closed first, without a log_stop: the
    /// session is gone, and anything written after lands in a new file.
    /// </summary>
    public Task RetireSessionAsync(string sessionId)
    {
        lock (_lock)
        {
            if (_files.Remove(sessionId, out var stale))
            {
                stale.Dispose();
            }

            RecordingInstance.RetireForeign(PathFor(sessionId), sessionId, null);
        }

        return Task.CompletedTask;
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
