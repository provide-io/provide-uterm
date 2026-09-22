//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests;

/// <summary>
/// A closed transport says who closed it, and the session keeps that answer.
/// Port of packages/provide-uterm/tests/test_transport_close.py and the pure
/// mapping half of provide-uterm-client/tests/transports/test_transport_close_mapping.py
/// (issue #102). The cases every port shares (close_cases in spec/behavior_vectors.json)
/// are in <see cref="TermSessionTransportCloseVectorTests"/>; these are the C#-specific
/// ones. Before this, WebSocketTransport and TelnetTransport threw the shared untyped
/// TransportErrors.ConnectionClosed and the session reader swallowed it.
/// </summary>
public class TermSessionTransportCloseTests
{
    // ---- TransportClose / TransportClosedException ----------------------------

    [Fact]
    public void TheClosedExceptionIsAnIOExceptionCarryingTheClose()
    {
        var close = new TransportClose(CloseInitiator.Local, 1011, "keepalive ping timeout");
        var err = new TransportClosedException("Connection closed", close);
        Assert.IsAssignableFrom<IOException>(err);
        Assert.Same(close, err.Close);
        Assert.Equal("Connection closed (local close 1011 keepalive ping timeout)", err.Message);
        Assert.Null(err.InnerException);
    }

    [Fact]
    public void TheClosedExceptionKeepsItsCause()
    {
        var cause = new IOException("boom");
        var err = new TransportClosedException("x", new TransportClose(CloseInitiator.Unknown), cause);
        Assert.Same(cause, err.InnerException);
    }

    [Theory]
    [InlineData(CloseInitiator.Local, "local")]
    [InlineData(CloseInitiator.Remote, "remote")]
    [InlineData(CloseInitiator.Unknown, "unknown")]
    public void InitiatorsSpellLikePython(CloseInitiator initiator, string name) =>
        Assert.Equal(name, TransportClose.InitiatorName(initiator));

    [Fact]
    public void AnExceptionCloseDefaultsToUnknownWithTypeAndMessage()
    {
        var close = TransportClose.FromException(new InvalidOperationException("peer reset"));
        Assert.Equal(new TransportClose(CloseInitiator.Unknown, Detail: "InvalidOperationException: peer reset"), close);
        Assert.Equal(
            CloseInitiator.Local,
            TransportClose.FromException(new IOException("x"), CloseInitiator.Local).Initiator);
    }

    // ---- WebSocket (shared frame attribution: TermSessionTransportCloseVectorTests) ----

    [Fact]
    public void AFailedWebSocketOperationPrefersTheReceivedStatus()
    {
        var ex = new WebSocketException(WebSocketError.InvalidState, "closed");
        var close = WebSocketTransport.CloseFromFrames(WebSocketCloseStatus.EndpointUnavailable, null, null, ex);
        Assert.Equal(new TransportClose(CloseInitiator.Remote, 1001, "", "WebSocketException: closed"), close);

        var local = WebSocketTransport.CloseFromFrames(null, null, WebSocketTransport.DisconnectFrame, ex);
        Assert.Equal("local close 1000 bye (WebSocketException: closed)", local.Summary());

        var unknown = WebSocketTransport.CloseFromFrames(null, "ignored", null, ex);
        Assert.Equal("unknown close (WebSocketException: closed)", unknown.Summary());

        var withReason = WebSocketTransport.CloseFromFrames(WebSocketCloseStatus.NormalClosure, "done", null, ex);
        Assert.Equal("done", withReason.Reason);
    }

    [Fact]
    public void AWebSocketFailureIsAClosedConnectionOnlyWhenItLooksLikeOne()
    {
        var premature = new WebSocketException(WebSocketError.ConnectionClosedPrematurely);
        var other = new WebSocketException(WebSocketError.Faulted);
        var unknown = new TransportClose(CloseInitiator.Unknown);
        Assert.Equal("connection closed", WebSocketTransport.ReceiveFailurePrefix(premature, unknown));
        Assert.Equal("WebSocket receive error", WebSocketTransport.ReceiveFailurePrefix(other, unknown));
        Assert.Equal("WebSocket receive error", WebSocketTransport.ReceiveFailurePrefix(new IOException(), unknown));
        Assert.Equal(
            "connection closed",
            WebSocketTransport.ReceiveFailurePrefix(other, new TransportClose(CloseInitiator.Remote, 1000)));
    }

    [Fact]
    public void OnlySocketFailuresBecomeWebSocketCloses()
    {
        Assert.True(WebSocketTransport.IsSocketFailure(new WebSocketException()));
        Assert.True(WebSocketTransport.IsSocketFailure(new IOException()));
        Assert.True(WebSocketTransport.IsSocketFailure(new ObjectDisposedException("ws")));
        Assert.False(WebSocketTransport.IsSocketFailure(new InvalidOperationException()));
        Assert.False(WebSocketTransport.IsSocketFailure(
            new TransportClosedException("x", new TransportClose(CloseInitiator.Remote))));
    }

    // ---- stream sockets (telnet) --------------------------------------------------

    [Fact]
    public void APeerResetIsARemoteClose()
    {
        var reset = new IOException("read failed", new SocketException((int)SocketError.ConnectionReset));
        var close = TransportClose.FromSocketError(reset);
        Assert.Equal(CloseInitiator.Remote, close.Initiator);
        Assert.StartsWith("SocketException: ", close.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void ABareSocketResetIsARemoteClose() =>
        Assert.Equal(
            CloseInitiator.Remote,
            TransportClose.FromSocketError(new SocketException((int)SocketError.ConnectionReset)).Initiator);

    [Fact]
    public void ABrokenPipeCannotSayWhoClosed()
    {
        var pipe = new IOException("write failed", new SocketException((int)SocketError.Shutdown));
        Assert.Equal(CloseInitiator.Unknown, TransportClose.FromSocketError(pipe).Initiator);
    }

    [Fact]
    public void AStreamErrorWithoutASocketCauseNamesTheStreamError()
    {
        var close = TransportClose.FromSocketError(new IOException("gone"));
        Assert.Equal(new TransportClose(CloseInitiator.Unknown, Detail: "IOException: gone"), close);
    }

    // ---- SSH (rule proposed in issue #102) ---------------------------------------

    [Fact]
    public void SshChannelEndIsRemoteUnlessWeDisconnected()
    {
        Assert.Equal(new TransportClose(CloseInitiator.Remote), SshTransport.EndOfStreamClose(false));
        Assert.Equal(new TransportClose(CloseInitiator.Local), SshTransport.EndOfStreamClose(true));
    }

    [Fact]
    public void SshFailuresAreUnknownUnlessWeDisconnected()
    {
        var ex = new Renci.SshNet.Common.SshConnectionException("aborted");
        Assert.Equal(
            new TransportClose(CloseInitiator.Unknown, Detail: "SshConnectionException: aborted"),
            SshTransport.CloseFromFailure(ex, false));
        Assert.Equal(CloseInitiator.Local, SshTransport.CloseFromFailure(ex, true).Initiator);
        Assert.True(SshTransport.IsSessionFailure(ex));
        Assert.True(SshTransport.IsSessionFailure(new IOException()));
        Assert.True(SshTransport.IsSessionFailure(new ObjectDisposedException("shell")));
        Assert.False(SshTransport.IsSessionFailure(new InvalidOperationException()));
        Assert.False(SshTransport.IsSessionFailure(
            new TransportClosedException("x", new TransportClose(CloseInitiator.Remote))));
    }

    // ---- TransportSession.CloseInfo ----------------------------------------------

    /// <summary>Yields one chunk, then throws <c>failure</c> on every later read (or idles when null).</summary>
    private sealed class EndsWith : IConnectionTransport
    {
        private readonly Exception? _failure;
        private int _receives;

        public EndsWith(Exception? failure) => _failure = failure;

        public int Disconnects { get; private set; }

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _receives = 0;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            Disconnects++;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            if (Interlocked.Increment(ref _receives) == 1)
            {
                return Encoding.ASCII.GetBytes("hello");
            }

            if (_failure is null)
            {
                await Task.Delay(10, cancellationToken);
                return Array.Empty<byte>();
            }

            throw _failure;
        }

        public bool IsConnected() => true;
    }

    private static TransportSession SessionOver(IConnectionTransport transport) =>
        new(transport, ct => transport.ConnectAsync("", 0, null, ct));

    private static async Task UntilDisconnected(TransportSession session)
    {
        for (var i = 0; i < 400; i++)
        {
            if (!session.IsConnected())
            {
                return;
            }

            await Task.Delay(5);
        }

        throw new Xunit.Sdk.XunitException("the reader never noticed the transport close");
    }

    [Fact]
    public void ANewSessionHasNoClose() => Assert.Null(SessionOver(new EndsWith(null)).CloseInfo);

    [Fact]
    public async Task TheReaderKeepsTheTransportClose()
    {
        var close = new TransportClose(CloseInitiator.Remote, 1001, "going away");
        var session = SessionOver(new EndsWith(new TransportClosedException("connection closed", close)));
        await session.ConnectAsync();
        await UntilDisconnected(session);
        Assert.Equal(close, session.CloseInfo);
        await session.CloseAsync();
        Assert.Equal(close, session.CloseInfo); // closing afterwards keeps the transport's close
    }

    [Fact]
    public async Task AnUntypedDropIsAnUnknownCloseWithItsDetail()
    {
        var session = SessionOver(new EndsWith(new IOException("peer reset")));
        await session.ConnectAsync();
        await UntilDisconnected(session);
        Assert.Equal(new TransportClose(CloseInitiator.Unknown, Detail: "IOException: peer reset"), session.CloseInfo);
    }

    [Fact]
    public async Task ClosingTheSessionIsALocalClose()
    {
        var transport = new EndsWith(null);
        var session = SessionOver(transport);
        await session.ConnectAsync();
        await session.CloseAsync();
        Assert.Same(TransportSession.ClosedByClient, session.CloseInfo);
        Assert.Equal("local close (closed by client)", session.CloseInfo!.Summary());
        Assert.Equal(1, transport.Disconnects);
    }

    [Fact]
    public async Task ConnectingAgainForgetsThePreviousClose()
    {
        var session = SessionOver(new EndsWith(null));
        await session.ConnectAsync();
        await session.CloseAsync();
        Assert.NotNull(session.CloseInfo);
        await session.ConnectAsync();
        Assert.Null(session.CloseInfo);
        await session.CloseAsync();
    }
}
