//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;

namespace Provide.Uterm.Transports;

/// <summary>Client WebSocket transport for terminal streams.</summary>
public sealed class WebSocketTransport : IConnectionTransport, IAsyncDisposable
{
    /// <summary>Hard cap on a single reassembled message (bytes).</summary>
    public const int DefaultMaxMessageBytes = 1 * 1024 * 1024;

    private ClientWebSocket? _ws;
    private readonly object _lock = new();
    private ConnectOptions _options = new();
    private int _maxMessageBytes = DefaultMaxMessageBytes;

    public int MaxMessageBytes
    {
        get => _maxMessageBytes;
        set => _maxMessageBytes = value <= 0 ? DefaultMaxMessageBytes : value;
    }

    public async Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        options = (options ?? new ConnectOptions()).WithDefaults();
        _options = options;
        var url = options.Ws.Url;
        if (string.IsNullOrEmpty(url))
        {
            url = $"wss://{host}:{port}";
        }

        // Scheme gate
        if (!url.StartsWith("ws://", StringComparison.OrdinalIgnoreCase) &&
            !url.StartsWith("wss://", StringComparison.OrdinalIgnoreCase))
        {
            throw new ArgumentException("WebSocket URL must use ws:// or wss:// scheme", nameof(options));
        }

        var ws = new ClientWebSocket();
        if (!string.IsNullOrEmpty(options.Ws.Origin))
        {
            ws.Options.SetRequestHeader("Origin", options.Ws.Origin);
        }

        foreach (var (k, v) in options.Ws.Headers)
        {
            ws.Options.SetRequestHeader(k, v);
        }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(options.Timeout);
        await ws.ConnectAsync(new Uri(url), cts.Token);
        lock (_lock)
        {
            _ws = ws;
        }
    }

    public async Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        ClientWebSocket? ws;
        lock (_lock)
        {
            ws = _ws;
            _ws = null;
        }

        if (ws is null)
        {
            return;
        }

        try
        {
            if (ws.State == WebSocketState.Open)
            {
                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", cancellationToken);
            }
        }
        catch
        {
            // ignore close races
        }

        ws.Dispose();
    }

    public async Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
    {
        ClientWebSocket ws;
        lock (_lock)
        {
            ws = _ws ?? throw TransportErrors.NotConnected;
        }

        if (data.Length > _maxMessageBytes)
        {
            throw new InvalidOperationException(
                $"WebSocket message size {data.Length} exceeds max {_maxMessageBytes}");
        }

        var type = _options.Ws.SendBinary ? WebSocketMessageType.Binary : WebSocketMessageType.Text;
        await ws.SendAsync(data, type, endOfMessage: true, cancellationToken);
    }

    public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        ClientWebSocket ws;
        lock (_lock)
        {
            ws = _ws ?? throw TransportErrors.NotConnected;
        }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(timeout);
        var chunk = new byte[Math.Max(1, Math.Min(maxBytes, 64 * 1024))];
        using var ms = new MemoryStream();
        try
        {
            while (true)
            {
                var result = await ws.ReceiveAsync(chunk, cts.Token);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    throw TransportErrors.ConnectionClosed;
                }

                if (result.Count > 0)
                {
                    if (ms.Length + result.Count > _maxMessageBytes)
                    {
                        throw new InvalidOperationException(
                            $"WebSocket reassembled message exceeds max {_maxMessageBytes}");
                    }

                    ms.Write(chunk, 0, result.Count);
                }

                if (result.EndOfMessage)
                {
                    break;
                }
            }

            return ms.ToArray();
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Array.Empty<byte>();
        }
    }

    public bool IsConnected()
    {
        lock (_lock)
        {
            return _ws is { State: WebSocketState.Open };
        }
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync();
}
