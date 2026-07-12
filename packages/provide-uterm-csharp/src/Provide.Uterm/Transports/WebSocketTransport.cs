//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;

namespace Provide.Uterm.Transports;

/// <summary>Client WebSocket transport for terminal streams.</summary>
public sealed class WebSocketTransport : IConnectionTransport, IAsyncDisposable
{
    private ClientWebSocket? _ws;
    private readonly object _lock = new();
    private ConnectOptions _options = new();

    public async Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        options = (options ?? new ConnectOptions()).WithDefaults();
        _options = options;
        var url = options.Ws.Url;
        if (string.IsNullOrEmpty(url))
        {
            url = $"wss://{host}:{port}";
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
        var buf = new byte[Math.Max(1, maxBytes)];
        try
        {
            var result = await ws.ReceiveAsync(buf, cts.Token);
            if (result.MessageType == WebSocketMessageType.Close)
            {
                throw TransportErrors.ConnectionClosed;
            }

            return buf[..result.Count];
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
