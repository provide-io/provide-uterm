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

    // The close frame this side sent, recorded before it is sent so a receive that
    // is already waiting attributes the close to us. Reset on every connect.
    private CloseFrame? _sentClose;

    /// <summary>Status and reason <see cref="DisconnectAsync"/> sends in its close frame.</summary>
    public static readonly CloseFrame DisconnectFrame = new((int)WebSocketCloseStatus.NormalClosure, "bye");

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
            _sentClose = null;
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
                Volatile.Write(ref _sentClose, DisconnectFrame);
                await ws.CloseAsync(
                    (WebSocketCloseStatus)DisconnectFrame.Code, DisconnectFrame.Reason, cancellationToken);
            }
            else if (ws.State == WebSocketState.CloseReceived)
            {
                // Answer the peer's close frame; the close is still theirs.
                await ws.CloseOutputAsync(
                    (WebSocketCloseStatus)DisconnectFrame.Code, DisconnectFrame.Reason, cancellationToken);
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
        try
        {
            await ws.SendAsync(data, type, endOfMessage: true, cancellationToken);
        }
        catch (Exception ex) when (IsSocketFailure(ex))
        {
            var close = CloseFromSocket(ws, ex);
            await DisconnectAsync(CancellationToken.None);
            throw new TransportClosedException("connection closed", close, ex);
        }
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
                    var received = new CloseFrame(
                        (int)(result.CloseStatus ?? WebSocketCloseStatus.Empty), result.CloseStatusDescription ?? "");
                    var close = TransportClose.FromWebSocketFrames(
                        received, Volatile.Read(ref _sentClose), receivedThenSent: false);
                    await DisconnectAsync(CancellationToken.None);
                    throw new TransportClosedException("connection closed", close);
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
        catch (Exception ex) when (IsSocketFailure(ex))
        {
            var close = CloseFromSocket(ws, ex);
            await DisconnectAsync(CancellationToken.None);
            throw new TransportClosedException(ReceiveFailurePrefix(ex, close), close, ex);
        }
    }

    /// <summary>A send or receive failure that means the socket is gone (not a size or state misuse).</summary>
    internal static bool IsSocketFailure(Exception ex) =>
        ex is WebSocketException or ObjectDisposedException or IOException && ex is not TransportClosedException;

    /// <summary>
    /// Python maps websockets' ConnectionClosed to "Connection closed" and any other
    /// receive failure to "WebSocket receive error". .NET has no ConnectionClosed; its
    /// equivalents are a failure once a close frame was exchanged, and a peer that
    /// dropped the TCP connection without one.
    /// </summary>
    internal static string ReceiveFailurePrefix(Exception ex, TransportClose close) =>
        close.Code is not null
        || ex is WebSocketException { WebSocketErrorCode: WebSocketError.ConnectionClosedPrematurely }
            ? "connection closed"
            : "WebSocket receive error";

    private TransportClose CloseFromSocket(ClientWebSocket ws, Exception ex) =>
        CloseFromFrames(ws.CloseStatus, ws.CloseStatusDescription, Volatile.Read(ref _sentClose), ex);

    /// <summary>
    /// The close a failed send or receive reports: the peer's close frame when the socket
    /// recorded one (<see cref="WebSocket.CloseStatus"/> is the status it received), else
    /// the one this side sent, else unknown; the failure is the detail.
    /// </summary>
    internal static TransportClose CloseFromFrames(
        WebSocketCloseStatus? receivedStatus, string? receivedReason, CloseFrame? sent, Exception ex)
    {
        var received = receivedStatus is { } status ? new CloseFrame((int)status, receivedReason ?? "") : null;
        return TransportClose.FromWebSocketFrames(
            received, sent, receivedThenSent: false, TransportClose.FromException(ex).Detail);
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
