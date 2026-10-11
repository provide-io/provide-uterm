//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Threading.Channels;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// An in-memory <see cref="WebSocket"/> whose peer is the test: the test queues
/// what the socket will receive and reads back what it sent, and can make any
/// operation fail. Unlike <see cref="ClientWebSocket"/>, a cancelled receive
/// leaves the socket Open (unless <see cref="AbortOnCancel"/>), so a pump's
/// close-after-cancel arm is reachable deterministically.
/// </summary>
internal sealed class ScriptedWebSocket : WebSocket
{
    private readonly Channel<(WebSocketMessageType Type, byte[] Data, bool End, Exception? Error)> _inbox =
        Channel.CreateUnbounded<(WebSocketMessageType, byte[], bool, Exception?)>();
    private readonly List<(WebSocketMessageType Type, byte[] Data)> _sent = new();
    private WebSocketState _state = WebSocketState.Open;
    private WebSocketCloseStatus? _closeStatus;

    public Exception? SendError { get; set; }
    public Exception? CloseError { get; set; }
    public bool AbortOnCancel { get; set; }
    public int CloseCalls { get; private set; }
    public TaskCompletionSource FirstReceivePending { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public IReadOnlyList<(WebSocketMessageType Type, byte[] Data)> Sent
    {
        get { lock (_sent) return _sent.ToList(); }
    }

    public void Enqueue(WebSocketMessageType type, byte[] data, bool endOfMessage = true) =>
        _inbox.Writer.TryWrite((type, data, endOfMessage, null));

    public void EnqueueText(string text) =>
        Enqueue(WebSocketMessageType.Text, System.Text.Encoding.UTF8.GetBytes(text));

    public void EnqueueClose() => Enqueue(WebSocketMessageType.Close, Array.Empty<byte>());

    public void EnqueueError(Exception ex) => _inbox.Writer.TryWrite((WebSocketMessageType.Binary, Array.Empty<byte>(), true, ex));

    public void SetState(WebSocketState state) => _state = state;

    public override WebSocketCloseStatus? CloseStatus => _closeStatus;
    public override string? CloseStatusDescription => null;
    public override WebSocketState State => _state;
    public override string? SubProtocol => null;

    public override void Abort() => _state = WebSocketState.Aborted;

    public override Task CloseAsync(WebSocketCloseStatus closeStatus, string? statusDescription, CancellationToken cancellationToken)
    {
        CloseCalls++;
        if (CloseError is not null)
        {
            return Task.FromException(CloseError);
        }

        _closeStatus = closeStatus;
        _state = WebSocketState.Closed;
        return Task.CompletedTask;
    }

    public override Task CloseOutputAsync(WebSocketCloseStatus closeStatus, string? statusDescription, CancellationToken cancellationToken)
    {
        CloseCalls++;
        if (CloseError is not null)
        {
            return Task.FromException(CloseError);
        }

        _closeStatus = closeStatus;
        _state = _state == WebSocketState.CloseReceived ? WebSocketState.Closed : WebSocketState.CloseSent;
        return Task.CompletedTask;
    }

    public override void Dispose()
    {
        if (_state is WebSocketState.Open or WebSocketState.CloseReceived or WebSocketState.CloseSent)
        {
            _state = WebSocketState.Closed;
        }
    }

    public override async Task<WebSocketReceiveResult> ReceiveAsync(ArraySegment<byte> buffer, CancellationToken cancellationToken)
    {
        FirstReceivePending.TrySetResult();
        (WebSocketMessageType Type, byte[] Data, bool End, Exception? Error) item;
        try
        {
            item = await _inbox.Reader.ReadAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            if (AbortOnCancel)
            {
                _state = WebSocketState.Aborted;
            }

            throw;
        }

        if (item.Error is not null)
        {
            throw item.Error;
        }

        if (item.Type == WebSocketMessageType.Close)
        {
            _state = _state == WebSocketState.CloseSent ? WebSocketState.Closed : WebSocketState.CloseReceived;
            _closeStatus = WebSocketCloseStatus.NormalClosure;
            return new WebSocketReceiveResult(0, WebSocketMessageType.Close, true, WebSocketCloseStatus.NormalClosure, null);
        }

        item.Data.CopyTo(buffer.Array!, buffer.Offset);
        return new WebSocketReceiveResult(item.Data.Length, item.Type, item.End);
    }

    public override Task SendAsync(ArraySegment<byte> buffer, WebSocketMessageType messageType, bool endOfMessage, CancellationToken cancellationToken)
    {
        if (SendError is not null)
        {
            return Task.FromException(SendError);
        }

        lock (_sent)
        {
            _sent.Add((messageType, buffer.ToArray()));
        }

        return Task.CompletedTask;
    }
}
