//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.RegularExpressions;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Emulator;
using Provide.Uterm.Session;
using Provide.Uterm.Transports;

namespace Provide.Uterm.TermSession;

public delegate void WatchFunc(IReadOnlyDictionary<string, object?> state, byte[] raw);
public delegate void ControlFrameFunc(IReadOnlyDictionary<string, object?> payload);

/// <summary>Options for <see cref="TransportSession"/>.</summary>
public sealed class TransportSessionOptions
{
    public int Cols { get; set; } = 80;
    public int Rows { get; set; } = 25;
    public bool ControlFrames { get; set; }
}

/// <summary>Telnet-specific session options (same fields as <see cref="ConnectOptions"/>).</summary>
public sealed class TelnetOptions
{
    public int Cols { get; set; }
    public int Rows { get; set; }
    public string Term { get; set; } = "";
    public TimeSpan Timeout { get; set; }

    public ConnectOptions ToConnectOptions()
    {
        return new ConnectOptions
        {
            Cols = Cols,
            Rows = Rows,
            Term = Term,
            Timeout = Timeout,
        }.WithDefaults();
    }
}

/// <summary>
/// Combines a <see cref="IConnectionTransport"/> with terminal emulation,
/// screen-change sequencing, watchers, and optional DLE/STX control frames.
/// Port of packages/provide-uterm-go/termsession.
/// </summary>
public sealed class TransportSession : IAsyncDisposable
{
    private readonly IConnectionTransport _transport;
    private readonly Func<CancellationToken, Task> _connect;
    private readonly int _cols;
    private readonly int _rows;
    private readonly TerminalEmulator _emu;
    private readonly ControlChannel.Decoder? _controlDecoder;
    private readonly object _gate = new();
    private readonly List<WatchFunc> _watchers = new();
    private readonly List<ControlFrameFunc> _controlWatchers = new();
    private bool _connected;
    private int _changeSeq;
    private int _updateSeq;
    private CancellationTokenSource? _readerCts;
    private Task? _readerTask;
    private TaskCompletionSource _updateTcs = NewTcs();
    private TaskCompletionSource _changeTcs = NewTcs();

    public TransportSession(
        IConnectionTransport transport,
        Func<CancellationToken, Task> connect,
        TransportSessionOptions? options = null)
    {
        _transport = transport;
        _connect = connect;
        options ??= new TransportSessionOptions();
        _cols = options.Cols > 0 ? options.Cols : 80;
        _rows = options.Rows > 0 ? options.Rows : 25;
        _emu = new TerminalEmulator(_cols, _rows);
        if (options.ControlFrames)
        {
            _controlDecoder = new ControlChannel.Decoder();
        }
    }

    public bool IsConnected()
    {
        lock (_gate) return _connected && _transport.IsConnected();
    }

    public int ScreenChangeSeq()
    {
        lock (_gate) return _changeSeq;
    }

    public int UpdateSeq()
    {
        lock (_gate) return _updateSeq;
    }

    public TerminalEmulator Emulator() => _emu;

    public static TransportSession ConnectTelnet(string host, int port, TransportSessionOptions? options = null)
    {
        var transport = new TelnetTransport();
        var opts = new ConnectOptions
        {
            Cols = options?.Cols ?? 80,
            Rows = options?.Rows ?? 25,
        };
        return new TransportSession(transport, ct => transport.ConnectAsync(host, port, opts, ct), options);
    }

    public static TransportSession ConnectWS(string url, TransportSessionOptions? options = null)
    {
        var transport = new WebSocketTransport();
        var opts = new ConnectOptions { Ws = new WsOptions { Url = url } };
        return new TransportSession(transport, ct => transport.ConnectAsync("", 0, opts, ct), options);
    }

    public async Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        await _connect(cancellationToken).ConfigureAwait(false);
        lock (_gate)
        {
            _connected = true;
            _readerCts = new CancellationTokenSource();
            _readerTask = Task.Run(() => ReaderLoopAsync(_readerCts.Token), CancellationToken.None);
        }
    }

    /// <summary>Go/spec-compatible alias for <see cref="ConnectAsync"/>.</summary>
    public Task Connect(CancellationToken cancellationToken = default) => ConnectAsync(cancellationToken);

    public async Task CloseAsync(CancellationToken cancellationToken = default)
    {
        CancellationTokenSource? cts;
        Task? reader;
        lock (_gate)
        {
            _connected = false;
            cts = _readerCts;
            reader = _readerTask;
            _readerCts = null;
            _readerTask = null;
        }

        cts?.Cancel();
        if (reader is not null)
        {
            try { await reader.ConfigureAwait(false); }
            catch { /* cancelled */ }
        }

        cts?.Dispose();
        await _transport.DisconnectAsync(cancellationToken).ConfigureAwait(false);
    }

    /// <summary>Go/spec-compatible alias for <see cref="CloseAsync"/>.</summary>
    public Task Close(CancellationToken cancellationToken = default) => CloseAsync(cancellationToken);

    public async Task SendAsync(string data, CancellationToken cancellationToken = default)
    {
        var bytes = Encoding.UTF8.GetBytes(data);
        await _transport.SendAsync(bytes, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>Go/spec-compatible alias for <see cref="SendAsync"/>.</summary>
    public Task Send(string data, CancellationToken cancellationToken = default) =>
        SendAsync(data, cancellationToken);

    public async Task<bool> SendExpectAsync(
        string data,
        string pattern,
        TimeSpan? timeout = null,
        CancellationToken cancellationToken = default)
    {
        await SendAsync(data, cancellationToken).ConfigureAwait(false);
        var deadline = DateTime.UtcNow + (timeout ?? TimeSpan.FromSeconds(5));
        var rx = new Regex(pattern, RegexOptions.Singleline);
        while (DateTime.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var text = ANSIScreen();
            if (rx.IsMatch(text)) return true;
            try
            {
                await WaitForUpdateAsync(TimeSpan.FromMilliseconds(100), cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException)
            {
                // continue
            }
        }

        return false;
    }

    /// <summary>Go/spec-compatible alias for <see cref="SendExpectAsync"/>.</summary>
    public Task<bool> SendExpect(
        string data,
        string pattern,
        TimeSpan? timeout = null,
        CancellationToken cancellationToken = default) =>
        SendExpectAsync(data, pattern, timeout, cancellationToken);

    public string ANSIScreen() => _emu.AnsiScreen();

    public Snapshot Snapshot() => _emu.GetSnapshot();

    /// <summary>Dictionary form of the snapshot for clients that expect map payloads.</summary>
    public Dictionary<string, object?> SnapshotDict()
    {
        var s = Snapshot();
        return new Dictionary<string, object?>
        {
            ["text"] = s.Screen,
            ["screen"] = s.Screen,
            ["cols"] = s.Cols,
            ["rows"] = s.Rows,
            ["change_seq"] = ScreenChangeSeq(),
            ["update_seq"] = UpdateSeq(),
        };
    }

    public void AddWatch(WatchFunc watcher)
    {
        lock (_gate) _watchers.Add(watcher);
    }

    public void AddControlFrameWatch(ControlFrameFunc watcher)
    {
        lock (_gate) _controlWatchers.Add(watcher);
    }

    public async Task WaitForUpdateAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default)
    {
        TaskCompletionSource tcs;
        lock (_gate) tcs = _updateTcs;
        var delay = timeout ?? TimeSpan.FromSeconds(5);
        var completed = await Task.WhenAny(tcs.Task, Task.Delay(delay, cancellationToken)).ConfigureAwait(false);
        if (completed != tcs.Task)
        {
            throw new TimeoutException("WaitForUpdate timed out");
        }
    }

    /// <summary>Go/spec-compatible wait; returns false on timeout instead of throwing.</summary>
    public async Task<bool> WaitForUpdate(TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        try
        {
            await WaitForUpdateAsync(timeout, cancellationToken).ConfigureAwait(false);
            return true;
        }
        catch (TimeoutException)
        {
            return false;
        }
    }

    public async Task WaitForScreenChangeAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default)
    {
        int start;
        lock (_gate) start = _changeSeq;
        var deadline = DateTime.UtcNow + (timeout ?? TimeSpan.FromSeconds(5));
        while (DateTime.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            lock (_gate)
            {
                if (_changeSeq != start) return;
            }

            try
            {
                await WaitForUpdateAsync(TimeSpan.FromMilliseconds(50), cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException)
            {
                // keep looping
            }
        }

        throw new TimeoutException("WaitForScreenChange timed out");
    }

    /// <summary>
    /// Go/spec-compatible wait. When <paramref name="since"/> &gt;= 0, returns true once
    /// the change sequence advances past it; a negative since waits for any next change.
    /// </summary>
    public async Task<bool> WaitForScreenChange(
        TimeSpan timeout, int since = -1, CancellationToken cancellationToken = default)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            int seq;
            TaskCompletionSource tcs;
            lock (_gate)
            {
                seq = _changeSeq;
                tcs = _changeTcs;
            }

            if (since >= 0 && seq > since)
            {
                return true;
            }

            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                lock (_gate)
                {
                    return _changeSeq > Math.Max(since, 0);
                }
            }

            var completed = await Task.WhenAny(tcs.Task, Task.Delay(remaining, cancellationToken))
                .ConfigureAwait(false);
            if (completed == tcs.Task && since < 0)
            {
                return true;
            }
        }
    }

    private async Task ReaderLoopAsync(CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                byte[] chunk;
                try
                {
                    chunk = await _transport.ReceiveAsync(4096, TimeSpan.FromMilliseconds(500), ct)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch
                {
                    break;
                }

                if (chunk.Length == 0)
                {
                    // timeout with no data — keep reading while connected
                    if (!_transport.IsConnected()) break;
                    continue;
                }

                ProcessIncoming(chunk);
            }
        }
        finally
        {
            lock (_gate) _connected = false;
        }
    }

    private void ProcessIncoming(byte[] chunk)
    {
        List<WatchFunc> watchers;
        List<ControlFrameFunc> controlWatchers;
        lock (_gate)
        {
            watchers = _watchers.ToList();
            controlWatchers = _controlWatchers.ToList();
        }

        if (_controlDecoder is not null)
        {
            var text = Encoding.UTF8.GetString(chunk);
            IReadOnlyList<Chunk> events;
            try
            {
                events = _controlDecoder.Feed(text);
            }
            catch
            {
                ApplyData(chunk);
                NotifyWatchers(watchers, chunk);
                return;
            }

            foreach (var ev in events)
            {
                switch (ev)
                {
                    case DataChunk d:
                        var dataBytes = Encoding.UTF8.GetBytes(d.Data);
                        ApplyData(dataBytes);
                        NotifyWatchers(watchers, dataBytes);
                        break;
                    case ControlChunk c:
                        foreach (var w in controlWatchers)
                        {
                            try { w(c.Payload); }
                            catch { /* ignore */ }
                        }

                        break;
                }
            }
        }
        else
        {
            ApplyData(chunk);
            NotifyWatchers(watchers, chunk);
        }
    }

    private static void NotifyWatchers(List<WatchFunc> watchers, byte[] chunk)
    {
        foreach (var w in watchers)
        {
            try { w(new Dictionary<string, object?>(), chunk); }
            catch { /* ignore */ }
        }
    }

    private void ApplyData(byte[] data)
    {
        var before = _emu.GetSnapshot().ScreenHash;
        _emu.Process(data);
        var after = _emu.GetSnapshot().ScreenHash;
        lock (_gate)
        {
            _updateSeq++;
            if (!string.Equals(before, after, StringComparison.Ordinal))
            {
                _changeSeq++;
                var oldChange = _changeTcs;
                _changeTcs = NewTcs();
                oldChange.TrySetResult();
            }

            var oldUpdate = _updateTcs;
            _updateTcs = NewTcs();
            oldUpdate.TrySetResult();
        }
    }

    private static TaskCompletionSource NewTcs() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public async ValueTask DisposeAsync() => await CloseAsync().ConfigureAwait(false);
}

/// <summary>Factory helpers matching Go termsession constructors.</summary>
public static class Sessions
{
    public static TransportSession NewTelnetSession(string host, int port, TelnetOptions? opts = null)
    {
        opts ??= new TelnetOptions();
        var connectOpts = opts.ToConnectOptions();
        var transport = new TelnetTransport();
        return new TransportSession(
            transport,
            ct => transport.ConnectAsync(host, port, connectOpts, ct),
            new TransportSessionOptions { Cols = connectOpts.Cols, Rows = connectOpts.Rows });
    }

    public static TransportSession NewWSSession(string url, WsOptions? opts = null) => NewWsSession(url, opts);

    public static TransportSession NewWsSession(string url, WsOptions? opts = null)
    {
        opts ??= new WsOptions { Url = url };
        if (string.IsNullOrEmpty(opts.Url)) opts.Url = url;
        var transport = new WebSocketTransport();
        var connectOpts = new ConnectOptions { Ws = opts };
        return new TransportSession(
            transport,
            ct => transport.ConnectAsync("", 0, connectOpts, ct),
            new TransportSessionOptions());
    }
}
