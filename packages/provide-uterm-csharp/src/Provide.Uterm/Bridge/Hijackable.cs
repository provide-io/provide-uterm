//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Bridge;

/// <summary>
/// Checkpoint hijackability primitives a worker embeds.
/// Port of packages/provide-uterm-go/bridge/hijackable.go.
/// </summary>
public sealed class Hijackable
{
    private readonly object _lock = new();
    private bool _hijacked;
    private int _stepTokens;
    private DateTime _lastProgress = DateTime.UtcNow;
    private TaskCompletionSource _gate = CreateOpenGate();
    private CancellationTokenSource? _watchdogCts;
    private Task? _watchdogTask;

    private static TaskCompletionSource CreateOpenGate()
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        tcs.TrySetResult();
        return tcs;
    }

    private static TaskCompletionSource CreateClosedGate() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public async Task AwaitIfHijacked(CancellationToken cancellationToken = default)
    {
        Task waitTask;
        lock (_lock)
        {
            if (!_hijacked)
            {
                return;
            }

            if (_stepTokens > 0)
            {
                _stepTokens--;
                return;
            }

            waitTask = _gate.Task;
        }

        await waitTask.WaitAsync(cancellationToken);
    }

    public void SetHijacked(bool enabled)
    {
        lock (_lock)
        {
            if (enabled == _hijacked)
            {
                return;
            }

            _hijacked = enabled;
            if (enabled)
            {
                _stepTokens = 0;
                _gate = CreateClosedGate();
            }
            else
            {
                _gate.TrySetResult();
                _gate = CreateOpenGate();
            }
        }
    }

    public void RequestStep(int tokens = 1)
    {
        lock (_lock)
        {
            if (!_hijacked)
            {
                return;
            }

            _stepTokens += Math.Max(1, tokens);
            _gate.TrySetResult();
            _gate = CreateClosedGate();
        }
    }

    public bool IsHijacked()
    {
        lock (_lock)
        {
            return _hijacked;
        }
    }

    public void MarkProgress()
    {
        lock (_lock)
        {
            _lastProgress = DateTime.UtcNow;
        }
    }

    public void StartWatchdog(TimeSpan? stuckTimeout = null, TimeSpan? checkInterval = null, Action? onStuck = null)
    {
        stuckTimeout ??= TimeSpan.FromSeconds(120);
        checkInterval ??= TimeSpan.FromSeconds(5);
        if (checkInterval < TimeSpan.FromMilliseconds(500))
        {
            checkInterval = TimeSpan.FromMilliseconds(500);
        }

        StopWatchdog();
        var cts = new CancellationTokenSource();
        _watchdogCts = cts;
        _watchdogTask = Task.Run(async () =>
        {
            while (!cts.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(checkInterval.Value, cts.Token);
                }
                catch (OperationCanceledException)
                {
                    break;
                }

                DateTime last;
                lock (_lock)
                {
                    last = _lastProgress;
                }

                if (DateTime.UtcNow - last >= stuckTimeout)
                {
                    try
                    {
                        onStuck?.Invoke();
                    }
                    catch
                    {
                    }
                }
            }
        }, cts.Token);
    }

    public void StopWatchdog()
    {
        var cts = _watchdogCts;
        _watchdogCts = null;
        if (cts is not null)
        {
            cts.Cancel();
            cts.Dispose();
        }
    }
}

/// <summary>Worker-side link contract (connection lifecycle hooks).</summary>
public interface IWorkerLink
{
    Task ConnectAsync(Uri hubUri, CancellationToken cancellationToken = default);
    Task DisconnectAsync(CancellationToken cancellationToken = default);
    Task SendTerminalAsync(byte[] data, CancellationToken cancellationToken = default);
    Task SendControlAsync(IReadOnlyDictionary<string, object?> frame, CancellationToken cancellationToken = default);
    bool IsConnected { get; }
}

/// <summary>Minimal TermBridge skeleton implementing IWorkerLink over ClientWebSocket.</summary>
public sealed class TermBridge : IWorkerLink, IAsyncDisposable
{
    private System.Net.WebSockets.ClientWebSocket? _ws;
    public bool IsConnected => _ws is { State: System.Net.WebSockets.WebSocketState.Open };

    public async Task ConnectAsync(Uri hubUri, CancellationToken cancellationToken = default)
    {
        var ws = new System.Net.WebSockets.ClientWebSocket();
        await ws.ConnectAsync(hubUri, cancellationToken);
        _ws = ws;
    }

    public async Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        if (_ws is { State: System.Net.WebSockets.WebSocketState.Open })
        {
            await _ws.CloseAsync(System.Net.WebSockets.WebSocketCloseStatus.NormalClosure, "bye", cancellationToken);
        }

        _ws?.Dispose();
        _ws = null;
    }

    public async Task SendTerminalAsync(byte[] data, CancellationToken cancellationToken = default)
    {
        if (_ws is null)
        {
            throw new InvalidOperationException("not connected");
        }

        var framed = System.Text.Encoding.UTF8.GetBytes(
            ControlChannel.ControlChannelCodec.EncodeTerminalData(
                Screen.Cp437.Decode(data)));
        await _ws.SendAsync(framed, System.Net.WebSockets.WebSocketMessageType.Text, true, cancellationToken);
    }

    public async Task SendControlAsync(IReadOnlyDictionary<string, object?> frame, CancellationToken cancellationToken = default)
    {
        if (_ws is null)
        {
            throw new InvalidOperationException("not connected");
        }

        var encoded = ControlChannel.ControlChannelCodec.EncodeControlFrame(frame);
        var bytes = System.Text.Encoding.UTF8.GetBytes(encoded);
        await _ws.SendAsync(bytes, System.Net.WebSockets.WebSocketMessageType.Text, true, cancellationToken);
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync();
}
