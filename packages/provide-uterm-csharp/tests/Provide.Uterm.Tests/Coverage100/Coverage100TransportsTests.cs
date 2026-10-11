//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The telnet and WebSocket client transports against loopback peers: IAC
/// escaping and stripping, timeouts, size caps, fragmented messages, and the
/// failure arms of send and disconnect.
/// </summary>
public sealed class Coverage100TransportsTests
{
    private const byte Iac = 255, Will = 251, Do = 253, Sb = 250, Se = 240, Nop = 241;

    // ---------------- telnet ----------------

    private static async Task<(TelnetTransport Tr, TcpClient Peer, TcpListener Listener)> TelnetAsync()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var accept = listener.AcceptTcpClientAsync();
        var tr = new TelnetTransport();
        await tr.ConnectAsync("127.0.0.1", ((IPEndPoint)listener.LocalEndpoint).Port);
        var peer = await accept;
        // The connect-time offer: WILL NAWS, WILL TTYPE (6), NAWS (9), TTYPE IS <term> (6 + term).
        var offer = new byte[6 + 9 + 6 + TransportDefaults.DefaultTerm.Length];
        await peer.GetStream().ReadExactlyAsync(offer).AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        return (tr, peer, listener);
    }

    private static async Task<byte[]> PeerSendsAsync(TelnetTransport tr, TcpClient peer, params byte[] bytes)
    {
        await peer.GetStream().WriteAsync(bytes);
        return await tr.ReceiveAsync(4096, TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task Telnet_strips_every_negotiation_shape_from_the_payload()
    {
        var (tr, peer, listener) = await TelnetAsync();
        using (peer)
        {
            Assert.Equal(new byte[] { (byte)'a', Iac, (byte)'b' },
                await PeerSendsAsync(tr, peer, (byte)'a', Iac, Iac, (byte)'b')); // escaped IAC
            Assert.Equal("cd"u8.ToArray(), await PeerSendsAsync(tr, peer, (byte)'c', Iac, Will, 1, (byte)'d')); // option command
            Assert.Equal("ef"u8.ToArray(), await PeerSendsAsync(tr, peer, (byte)'e', Iac, Sb, 24, 1, Iac, Se, (byte)'f')); // subnegotiation
            Assert.Equal("gh"u8.ToArray(), await PeerSendsAsync(tr, peer, (byte)'g', Iac, Nop, (byte)'h')); // two-byte command
            Assert.Equal("i"u8.ToArray(), await PeerSendsAsync(tr, peer, (byte)'i', Iac, Sb, 24, Iac)); // unterminated subnegotiation
            Assert.Equal("j"u8.ToArray(), await PeerSendsAsync(tr, peer, (byte)'j', Iac, Do)); // truncated option command
            Assert.Equal("k"u8.ToArray(), await PeerSendsAsync(tr, peer, (byte)'k', Iac)); // lone trailing IAC
        }

        await tr.DisposeAsync();
        listener.Stop();
    }

    [Fact]
    public async Task Telnet_escapes_iac_on_send_and_times_out_an_idle_read()
    {
        var (tr, peer, listener) = await TelnetAsync();
        using (peer)
        {
            await tr.SendAsync(new byte[] { 1, Iac, 2 });
            var got = new byte[4];
            await peer.GetStream().ReadExactlyAsync(got).AsTask().WaitAsync(TimeSpan.FromSeconds(10));
            Assert.Equal(new byte[] { 1, Iac, Iac, 2 }, got);

            Assert.Empty(await tr.ReceiveAsync(16, TimeSpan.FromMilliseconds(20)));
            Assert.True(tr.IsConnected());
        }

        await tr.DisposeAsync();
        listener.Stop();
    }

    [Fact]
    public async Task Telnet_send_to_a_reset_peer_reports_the_close_and_disconnects()
    {
        var (tr, peer, listener) = await TelnetAsync();
        peer.Client.LingerState = new LingerOption(true, 0);
        peer.Client.Close(); // RST (TcpClient.Close would shut down with a FIN first)
        // Writes into a reset connection fail once the reset has landed; the
        // first one may still be accepted by the local stack.
        TransportClosedException? closed = null;
        for (var i = 0; i < 1000 && closed is null; i++)
        {
            try
            {
                await tr.SendAsync(new byte[1024]);
                await Task.Yield();
            }
            catch (TransportClosedException ex)
            {
                closed = ex;
            }
        }

        Assert.NotNull(closed);
        Assert.StartsWith("send failed", closed!.Message, StringComparison.Ordinal);
        Assert.False(tr.IsConnected());
        await Assert.ThrowsAnyAsync<Exception>(() => tr.SendAsync(new byte[1]));
        listener.Stop();
    }

    [Fact]
    public async Task Telnet_reads_a_graceful_close_and_a_reset_as_closed_connections()
    {
        var (tr, peer, listener) = await TelnetAsync();
        peer.Close();
        var eof = await Assert.ThrowsAsync<TransportClosedException>(() => tr.ReceiveAsync(16, TimeSpan.FromSeconds(10)));
        Assert.StartsWith("connection closed by remote", eof.Message, StringComparison.Ordinal);
        Assert.Equal(CloseInitiator.Remote, eof.Close.Initiator);
        Assert.False(tr.IsConnected());

        var (tr2, peer2, _) = await TelnetAsync();
        peer2.Client.LingerState = new LingerOption(true, 0);
        peer2.Client.Close(); // RST (TcpClient.Close would shut down with a FIN first)
        var lost = await Assert.ThrowsAsync<TransportClosedException>(() => tr2.ReceiveAsync(16, TimeSpan.FromSeconds(10)));
        Assert.StartsWith("connection lost", lost.Message, StringComparison.Ordinal);
        Assert.False(tr2.IsConnected());
        listener.Stop();
    }

    // ---------------- WebSocket ----------------

    private static ConnectOptions Ws(string url, bool binary = false, string origin = "", Dictionary<string, string>? headers = null) =>
        new() { Ws = new WsOptions { Url = url, SendBinary = binary, Origin = origin, Headers = headers ?? new() } };

    private static async Task DrainAsync(WebSocket ws)
    {
        var buf = new byte[1024];
        try
        {
            while ((await ws.ReceiveAsync(buf, CancellationToken.None)).MessageType != WebSocketMessageType.Close)
            {
            }
        }
        catch (WebSocketException)
        {
            // the client went away without a close frame
        }
    }

    [Fact]
    public async Task WebSocket_without_a_url_dials_wss_on_host_and_port()
    {
        await using var tr = new WebSocketTransport();
        // Nothing listens there: the derived wss:// URL is dialled and refused.
        await Assert.ThrowsAnyAsync<Exception>(() => tr.ConnectAsync("127.0.0.1", C100Server.FreePort(), new ConnectOptions()));
        Assert.False(tr.IsConnected());
    }

    [Fact]
    public async Task WebSocket_sends_its_headers_reassembles_fragments_and_closes_cleanly()
    {
        var closedByClient = new TaskCompletionSource<WebSocketCloseStatus?>(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var server = await LoopbackWsServer.StartAsync(async (ws, ctx) =>
        {
            var hello = $"{ctx.Request.Headers.Origin}|{ctx.Request.Headers["X-C100"]}";
            await ws.SendAsync(Encoding.UTF8.GetBytes(hello), WebSocketMessageType.Text, true, CancellationToken.None);
            // One message in three frames, the middle one empty.
            await ws.SendAsync("fr"u8.ToArray(), WebSocketMessageType.Binary, false, CancellationToken.None);
            await ws.SendAsync(Array.Empty<byte>(), WebSocketMessageType.Binary, false, CancellationToken.None);
            await ws.SendAsync("ag"u8.ToArray(), WebSocketMessageType.Binary, true, CancellationToken.None);
            var buf = new byte[64];
            var r = await ws.ReceiveAsync(buf, CancellationToken.None);
            await ws.SendAsync(buf.AsMemory(0, r.Count), r.MessageType == WebSocketMessageType.Binary ? WebSocketMessageType.Binary : WebSocketMessageType.Text, true, CancellationToken.None);
            r = await ws.ReceiveAsync(buf, CancellationToken.None);
            closedByClient.TrySetResult(r.CloseStatus);
            await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "ok", CancellationToken.None);
        });

        await using var tr = new WebSocketTransport();
        await tr.ConnectAsync("ignored", 0, Ws(server.Url, binary: true, origin: "http://c100.test", headers: new() { ["X-C100"] = "yes" }));
        Assert.True(tr.IsConnected());
        Assert.Equal("http://c100.test|yes", Encoding.UTF8.GetString(await tr.ReceiveAsync(64, TimeSpan.FromSeconds(10))));
        Assert.Equal("frag", Encoding.UTF8.GetString(await tr.ReceiveAsync(2, TimeSpan.FromSeconds(10))));
        await tr.SendAsync("bin"u8.ToArray());
        Assert.Equal("bin", Encoding.UTF8.GetString(await tr.ReceiveAsync(64, TimeSpan.FromSeconds(10))));
        await tr.DisconnectAsync();
        Assert.Equal(WebSocketCloseStatus.NormalClosure, await closedByClient.Task.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.False(tr.IsConnected());
        await tr.DisconnectAsync(); // already disconnected: nothing to do
    }

    [Fact]
    public async Task WebSocket_size_caps_refuse_oversized_sends_and_reassemblies()
    {
        await using var server = await LoopbackWsServer.StartAsync(async (ws, _) =>
        {
            await ws.SendAsync(new byte[8], WebSocketMessageType.Binary, false, CancellationToken.None);
            await ws.SendAsync(new byte[8], WebSocketMessageType.Binary, true, CancellationToken.None);
            await DrainAsync(ws);
        });

        await using var tr = new WebSocketTransport { MaxMessageBytes = 12 };
        await tr.ConnectAsync("", 0, Ws(server.Url));
        var send = await Assert.ThrowsAsync<InvalidOperationException>(() => tr.SendAsync(new byte[13]));
        Assert.Contains("exceeds max 12", send.Message, StringComparison.Ordinal);
        var recv = await Assert.ThrowsAsync<InvalidOperationException>(() => tr.ReceiveAsync(8, TimeSpan.FromSeconds(10)));
        Assert.Contains("reassembled message exceeds max 12", recv.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task WebSocket_send_after_an_abandoned_read_reports_the_close()
    {
        await using var server = await LoopbackWsServer.StartAsync((ws, _) => DrainAsync(ws));
        await using var tr = new WebSocketTransport();
        await tr.ConnectAsync("", 0, Ws(server.Url));
        // A timed-out read reads as no data, and abandoning a pending receive
        // aborts a ClientWebSocket, so the next send fails as a closed socket.
        Assert.Empty(await tr.ReceiveAsync(16, TimeSpan.FromMilliseconds(20)));
        var ex = await Assert.ThrowsAsync<TransportClosedException>(() => tr.SendAsync("x"u8.ToArray()));
        Assert.StartsWith("connection closed", ex.Message, StringComparison.Ordinal);
        Assert.False(tr.IsConnected());
    }

    [Fact]
    public async Task WebSocket_read_after_an_abandoned_read_reports_a_receive_error()
    {
        await using var server = await LoopbackWsServer.StartAsync((ws, _) => DrainAsync(ws));
        await using var tr = new WebSocketTransport();
        await tr.ConnectAsync("", 0, Ws(server.Url));
        Assert.Empty(await tr.ReceiveAsync(16, TimeSpan.FromMilliseconds(20)));
        var ex = await Assert.ThrowsAsync<TransportClosedException>(() => tr.ReceiveAsync(16, TimeSpan.FromSeconds(10)));
        Assert.StartsWith("WebSocket receive error", ex.Message, StringComparison.Ordinal);
        Assert.False(tr.IsConnected());
    }

    [Fact]
    public async Task WebSocket_peer_close_is_answered_and_reported_as_theirs()
    {
        await using var server = await LoopbackWsServer.StartAsync(async (ws, _) =>
        {
            await ws.CloseOutputAsync(WebSocketCloseStatus.PolicyViolation, "go away", CancellationToken.None);
            await DrainAsync(ws);
        });
        await using var tr = new WebSocketTransport();
        Assert.Equal(WebSocketTransport.DefaultMaxMessageBytes, tr.MaxMessageBytes);
        tr.MaxMessageBytes = 0; // not a cap: the default stands
        Assert.Equal(WebSocketTransport.DefaultMaxMessageBytes, tr.MaxMessageBytes);
        await tr.ConnectAsync("", 0, Ws(server.Url));
        var ex = await Assert.ThrowsAsync<TransportClosedException>(() => tr.ReceiveAsync(16, TimeSpan.FromSeconds(10)));
        Assert.StartsWith("connection closed", ex.Message, StringComparison.Ordinal);
        Assert.Equal((int)WebSocketCloseStatus.PolicyViolation, ex.Close.Code);
        Assert.False(tr.IsConnected());
        await Assert.ThrowsAsync<ArgumentException>(() => tr.ConnectAsync("", 0, Ws("http://127.0.0.1/")));
    }

    [Fact]
    public async Task WebSocket_disconnect_ignores_a_close_that_cannot_be_sent()
    {
        await using var server = await LoopbackWsServer.StartAsync((ws, _) => DrainAsync(ws));
        var tr = new WebSocketTransport();
        await tr.ConnectAsync("", 0, Ws(server.Url));
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();
        await tr.DisconnectAsync(cancelled.Token); // the close is cancelled; the socket is still released
        Assert.False(tr.IsConnected());
        await Assert.ThrowsAnyAsync<Exception>(() => tr.SendAsync(new byte[1]));
    }
}
