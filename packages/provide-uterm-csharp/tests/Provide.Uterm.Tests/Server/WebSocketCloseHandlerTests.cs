//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Net.WebSockets;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

public sealed class WebSocketCloseHandlerTests
{
    [Fact]
    public async Task NonCompletingCloseOutputIsBoundedAndAborted()
    {
        using var socket = new CloseSocket(completeClose: false);
        var watch = Stopwatch.StartNew();

        await WebSocketCloseHandler.CloseAndTerminateAsync(
            socket, WebSocketCloseStatus.ProtocolError, "bad", TimeSpan.FromMilliseconds(40));

        Assert.True(watch.Elapsed < TimeSpan.FromMilliseconds(500), $"close took {watch.Elapsed}");
        Assert.True(socket.CloseOutputCalled);
        Assert.True(socket.Aborted);
    }

    [Fact]
    public async Task ReceivedCloseIsAcknowledgedWithoutAbortWhenPeerCompletes()
    {
        using var socket = new CloseSocket(completeClose: true, WebSocketState.CloseReceived);

        await WebSocketCloseHandler.CloseAndTerminateAsync(
            socket, WebSocketCloseStatus.NormalClosure, "bye", TimeSpan.FromMilliseconds(40));

        Assert.True(socket.CloseOutputCalled);
        Assert.False(socket.Aborted);
        Assert.Equal(WebSocketState.Closed, socket.State);
    }

    [Fact]
    public async Task MissingPeerAcknowledgementIsWaitedForOnlyUntilDeadline()
    {
        using var socket = new CloseSocket(completeClose: true, closeOutputEndsSocket: false);
        var watch = Stopwatch.StartNew();

        await WebSocketCloseHandler.CloseAndTerminateAsync(
            socket, WebSocketCloseStatus.NormalClosure, "bye", TimeSpan.FromMilliseconds(40));

        Assert.True(watch.Elapsed >= TimeSpan.FromMilliseconds(30), $"close took {watch.Elapsed}");
        Assert.True(watch.Elapsed < TimeSpan.FromMilliseconds(500), $"close took {watch.Elapsed}");
        Assert.True(socket.Aborted);
    }

    private sealed class CloseSocket : WebSocket
    {
        private readonly bool _completeClose;
        private readonly TaskCompletionSource _never = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<WebSocketReceiveResult> _neverReceive =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly bool _closeOutputEndsSocket;
        private WebSocketState _state;

        public CloseSocket(
            bool completeClose,
            WebSocketState state = WebSocketState.Open,
            bool closeOutputEndsSocket = true)
        {
            _completeClose = completeClose;
            _state = state;
            _closeOutputEndsSocket = closeOutputEndsSocket;
        }

        public bool CloseOutputCalled { get; private set; }
        public bool Aborted { get; private set; }
        public override WebSocketCloseStatus? CloseStatus => null;
        public override string? CloseStatusDescription => null;
        public override WebSocketState State => _state;
        public override string? SubProtocol => null;

        public override void Abort()
        {
            Aborted = true;
            _state = WebSocketState.Aborted;
        }

        public override Task CloseAsync(WebSocketCloseStatus closeStatus, string? statusDescription,
            CancellationToken cancellationToken) => CloseOutputAsync(closeStatus, statusDescription, cancellationToken);

        public override Task CloseOutputAsync(WebSocketCloseStatus closeStatus, string? statusDescription,
            CancellationToken cancellationToken)
        {
            CloseOutputCalled = true;
            if (!_completeClose) return _never.Task;
            _state = _closeOutputEndsSocket ? WebSocketState.Closed : WebSocketState.CloseSent;
            return Task.CompletedTask;
        }

        public override void Dispose() => _state = WebSocketState.Closed;

        public override Task<WebSocketReceiveResult> ReceiveAsync(ArraySegment<byte> buffer,
            CancellationToken cancellationToken) => _neverReceive.Task;

        public override Task SendAsync(ArraySegment<byte> buffer, WebSocketMessageType messageType,
            bool endOfMessage, CancellationToken cancellationToken) => throw new NotSupportedException();
    }
}
