//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Recording;
using Provide.Uterm.Redaction;

namespace Provide.Uterm.SessionLogger;

public enum ControlChannelMode
{
    Exclude,
    Wire,
}

public sealed class SessionLoggerOptions
{
    public int MaxBytes { get; set; }
    public ControlChannelMode ControlChannelMode { get; set; } = ControlChannelMode.Exclude;
    public Redactor? Redactor { get; set; }
    public TimeSpan FlushInterval { get; set; } = TimeSpan.FromSeconds(5);
    public int BatchSize { get; set; } = 100;
}

/// <summary>
/// Async session recorder over a pluggable recording store.
/// Port of provide.uterm.session_logger / packages/provide-uterm-go/sessionlogger.
/// </summary>
public sealed class SessionLogger : IAsyncDisposable
{
    private readonly IRecordingStore _store;
    private readonly int _maxBytes;
    private readonly ControlChannelMode _controlChannelMode;
    private readonly Redactor? _redactor;
    private readonly TimeSpan _flushInterval;
    private readonly int _batchSize;

    private readonly object _lock = new();
    private string _sessionId = "";
    private int _bytesWritten;
    private readonly List<Event> _buffer = new();
    private CancellationTokenSource? _flushCts;
    private Task? _flushTask;

    public SessionLogger(IRecordingStore store, SessionLoggerOptions? options = null)
    {
        options ??= new SessionLoggerOptions();
        _store = store;
        _maxBytes = options.MaxBytes;
        _controlChannelMode = options.ControlChannelMode;
        _redactor = options.Redactor;
        _flushInterval = options.FlushInterval <= TimeSpan.Zero
            ? TimeSpan.FromSeconds(5)
            : options.FlushInterval;
        _batchSize = options.BatchSize <= 0 ? 100 : options.BatchSize;
    }

    public async Task StartAsync(string sessionId)
    {
        lock (_lock)
        {
            _sessionId = sessionId;
        }

        var metadata = new Dictionary<string, object?>
        {
            ["started_at"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
        };
        await _store.StartSessionAsync(sessionId, metadata);
        var meta = await _store.RecordingMetaAsync(sessionId);
        lock (_lock)
        {
            _bytesWritten = (int)meta.SizeBytes;
            _flushCts = new CancellationTokenSource();
            _flushTask = PeriodicFlushAsync(_flushCts.Token);
        }
    }

    public async Task StopAsync()
    {
        CancellationTokenSource? cts;
        Task? task;
        lock (_lock)
        {
            cts = _flushCts;
            task = _flushTask;
            _flushCts = null;
            _flushTask = null;
        }

        if (cts is not null)
        {
            await cts.CancelAsync();
            if (task is not null)
            {
                try
                {
                    await task;
                }
                catch (OperationCanceledException)
                {
                }
            }

            cts.Dispose();
        }

        await FlushAsync();
        string sessionId;
        lock (_lock)
        {
            sessionId = _sessionId;
        }

        if (sessionId.Length > 0)
        {
            await _store.EndSessionAsync(sessionId);
        }
    }

    public async Task LogAsync(string eventName, IReadOnlyDictionary<string, object?> data)
    {
        if (_controlChannelMode == ControlChannelMode.Exclude &&
            (eventName is "wire" or "control"))
        {
            return;
        }

        var payload = new Dictionary<string, object?>(data);
        if (_redactor is not null)
        {
            foreach (var key in payload.Keys.ToList())
            {
                if (payload[key] is string s)
                {
                    payload[key] = Redaction.Redaction.RedactText(s, _redactor);
                }
            }
        }

        string sessionId;
        lock (_lock)
        {
            sessionId = _sessionId;
            if (_maxBytes > 0 && _bytesWritten >= _maxBytes)
            {
                return;
            }

            var evt = new Event
            {
                ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
                ["event"] = eventName,
                ["data"] = payload,
                ["session_id"] = sessionId,
            };
            _buffer.Add(evt);
            if (_buffer.Count >= _batchSize)
            {
                // fall through to flush
            }
            else
            {
                return;
            }
        }

        await FlushAsync();
    }

    public async Task FlushAsync()
    {
        List<Event> batch;
        string sessionId;
        lock (_lock)
        {
            if (_buffer.Count == 0)
            {
                return;
            }

            batch = _buffer.ToList();
            _buffer.Clear();
            sessionId = _sessionId;
        }

        if (sessionId.Length == 0)
        {
            return;
        }

        await _store.AppendEventsAsync(sessionId, batch);
        lock (_lock)
        {
            foreach (var e in batch)
            {
                _bytesWritten += Encoding.UTF8.GetByteCount(System.Text.Json.JsonSerializer.Serialize(e)) + 1;
            }
        }
    }

    private async Task PeriodicFlushAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(_flushInterval, ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }

            await FlushAsync();
        }
    }

    public async ValueTask DisposeAsync() => await StopAsync();
}
