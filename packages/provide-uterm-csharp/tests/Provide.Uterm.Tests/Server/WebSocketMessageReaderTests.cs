//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

public sealed class WebSocketMessageReaderTests
{
    [Fact]
    public async Task ReadAsync_AccumulatesFragmentsAndPreservesType()
    {
        using var ws = new ScriptedWebSocket(
            Fragment("he", WebSocketMessageType.Text, false),
            Fragment("llo", WebSocketMessageType.Text, true));

        var message = await WebSocketMessageReader.ReadAsync(ws, 5, CancellationToken.None);

        Assert.False(message.IsClose);
        Assert.Equal(WebSocketMessageType.Text, message.MessageType);
        Assert.Equal("hello", Encoding.UTF8.GetString(message.Payload));
    }

    [Fact]
    public async Task ReadAsync_PreservesBinaryPayload()
    {
        using var ws = new ScriptedWebSocket(
            Fragment([0, 1], WebSocketMessageType.Binary, false),
            Fragment([2, 255], WebSocketMessageType.Binary, true));

        var message = await WebSocketMessageReader.ReadAsync(ws, 4, CancellationToken.None);

        Assert.Equal(WebSocketMessageType.Binary, message.MessageType);
        Assert.Equal(new byte[] { 0, 1, 2, 255 }, message.Payload);
    }

    [Fact]
    public async Task ReadAsync_ReturnsCloseDistinctly()
    {
        using var ws = new ScriptedWebSocket(
            new ScriptedFragment([], WebSocketMessageType.Close, true,
                WebSocketCloseStatus.NormalClosure, "bye"));

        var message = await WebSocketMessageReader.ReadAsync(ws, 10, CancellationToken.None);

        Assert.True(message.IsClose);
        Assert.Equal(WebSocketCloseStatus.NormalClosure, message.CloseStatus);
        Assert.Equal("bye", message.CloseStatusDescription);
        Assert.Empty(message.Payload);
    }

    [Fact]
    public async Task ReadAsync_RefusesMessageTypeChangeAcrossFragments()
    {
        using var ws = new ScriptedWebSocket(
            Fragment("a", WebSocketMessageType.Text, false),
            Fragment([0x62], WebSocketMessageType.Binary, true));

        var error = await Assert.ThrowsAsync<WebSocketMessageException>(
            () => WebSocketMessageReader.ReadAsync(ws, 10, CancellationToken.None));

        Assert.Equal(WebSocketCloseStatus.InvalidMessageType, error.CloseStatus);
    }

    [Fact]
    public async Task ReadAsync_AcceptsExactLimitAndRefusesLimitPlusOne()
    {
        using var exact = new ScriptedWebSocket(
            Fragment("12", WebSocketMessageType.Text, false),
            Fragment("34", WebSocketMessageType.Text, true));
        Assert.Equal(4, (await WebSocketMessageReader.ReadAsync(exact, 4, CancellationToken.None)).Payload.Length);

        using var tooLarge = new ScriptedWebSocket(
            Fragment("12", WebSocketMessageType.Text, false),
            Fragment("345", WebSocketMessageType.Text, true));
        var error = await Assert.ThrowsAsync<WebSocketMessageException>(
            () => WebSocketMessageReader.ReadAsync(tooLarge, 4, CancellationToken.None));

        Assert.Equal(WebSocketCloseStatus.MessageTooBig, error.CloseStatus);
    }

    private static ScriptedFragment Fragment(string value, WebSocketMessageType type, bool end) =>
        Fragment(Encoding.UTF8.GetBytes(value), type, end);

    private static ScriptedFragment Fragment(byte[] value, WebSocketMessageType type, bool end) =>
        new(value, type, end, null, null);

    private sealed record ScriptedFragment(
        byte[] Payload,
        WebSocketMessageType Type,
        bool EndOfMessage,
        WebSocketCloseStatus? CloseStatus,
        string? CloseDescription);

    private sealed class ScriptedWebSocket(params ScriptedFragment[] fragments) : WebSocket
    {
        private readonly Queue<ScriptedFragment> _fragments = new(fragments);
        private WebSocketCloseStatus? _closeStatus;
        private string? _closeDescription;
        private WebSocketState _state = WebSocketState.Open;

        public override WebSocketCloseStatus? CloseStatus => _closeStatus;
        public override string? CloseStatusDescription => _closeDescription;
        public override WebSocketState State => _state;
        public override string? SubProtocol => null;

        public override void Abort() => _state = WebSocketState.Aborted;
        public override Task CloseAsync(WebSocketCloseStatus closeStatus, string? statusDescription,
            CancellationToken cancellationToken)
        {
            _state = WebSocketState.Closed;
            return Task.CompletedTask;
        }

        public override Task CloseOutputAsync(WebSocketCloseStatus closeStatus, string? statusDescription,
            CancellationToken cancellationToken) => CloseAsync(closeStatus, statusDescription, cancellationToken);

        public override void Dispose() => _state = WebSocketState.Closed;

        public override Task<WebSocketReceiveResult> ReceiveAsync(ArraySegment<byte> buffer,
            CancellationToken cancellationToken)
        {
            var fragment = _fragments.Dequeue();
            fragment.Payload.CopyTo(buffer.AsSpan());
            _closeStatus = fragment.CloseStatus;
            _closeDescription = fragment.CloseDescription;
            if (fragment.Type == WebSocketMessageType.Close) _state = WebSocketState.CloseReceived;
            return Task.FromResult(new WebSocketReceiveResult(
                fragment.Payload.Length,
                fragment.Type,
                fragment.EndOfMessage,
                fragment.CloseStatus,
                fragment.CloseDescription));
        }

        public override Task SendAsync(ArraySegment<byte> buffer, WebSocketMessageType messageType,
            bool endOfMessage, CancellationToken cancellationToken) => Task.CompletedTask;
    }
}
