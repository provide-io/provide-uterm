//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests;

/// <summary>
/// The real WebSocket and telnet transports against loopback peers: a close reaches
/// the caller, and the transport session, with the side that ended it. Port of the
/// live half of provide-uterm-client/tests/transports/test_transport_close_mapping.py.
/// </summary>
public class TermSessionTransportCloseLiveTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    // Telnet negotiation TelnetTransport.ConnectAsync writes with default options:
    // WILL NAWS + WILL TTYPE (6), NAWS 80x25 (9), TTYPE IS "ANSI" (10).
    private const int TelnetHandshakeBytes = 25;

    /// <summary>One-route WebSocket server on an ephemeral loopback port.</summary>
    private static async Task<(WebApplication App, string Url)> StartWsServer(Func<HttpContext, WebSocket, Task> onSocket)
    {
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel().UseUrls("http://127.0.0.1:0");
        var app = builder.Build();
        app.UseWebSockets();
        app.Map("/ws", async ctx =>
        {
            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            await onSocket(ctx, ws);
        });
        await app.StartAsync();
        var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.First();
        return (app, address.Replace("http://", "ws://", StringComparison.Ordinal) + "/ws");
    }

    private static Task ConnectWs(WebSocketTransport transport, string url) =>
        transport.ConnectAsync("", 0, new ConnectOptions { Ws = new WsOptions { Url = url } });

    [Fact]
    public async Task AServerCloseFrameIsARemoteCloseWithItsCodeAndReason()
    {
        var (app, url) = await StartWsServer(async (_, ws) =>
        {
            await ws.CloseOutputAsync(WebSocketCloseStatus.EndpointUnavailable, "going away", CancellationToken.None);
            // Stay until the client answers so the close handshake completes.
            try { await ws.ReceiveAsync(new byte[16], new CancellationTokenSource(Wait).Token); } catch { /* gone */ }
        });
        try
        {
            var transport = new WebSocketTransport();
            await ConnectWs(transport, url);
            var err = await Assert.ThrowsAsync<TransportClosedException>(
                () => transport.ReceiveAsync(1024, Wait));
            Assert.Equal(new TransportClose(CloseInitiator.Remote, 1001, "going away"), err.Close);
            Assert.Equal("connection closed (remote close 1001 going away)", err.Message);
            Assert.False(transport.IsConnected());
            // After the close the transport is disconnected: "not connected" is not a close.
            await Assert.ThrowsAsync<InvalidOperationException>(() => transport.ReceiveAsync(1024, Wait));
        }
        finally
        {
            await app.StopAsync();
        }
    }

    [Fact]
    public async Task AServerThatDropsTheSocketIsAnUnknownClose()
    {
        var (app, url) = await StartWsServer((ctx, _) =>
        {
            ctx.Abort();
            return Task.CompletedTask;
        });
        try
        {
            var transport = new WebSocketTransport();
            await ConnectWs(transport, url);
            var err = await Assert.ThrowsAsync<TransportClosedException>(
                () => transport.ReceiveAsync(1024, Wait));
            Assert.Equal(CloseInitiator.Unknown, err.Close.Initiator);
            Assert.Null(err.Close.Code);
            Assert.StartsWith("WebSocketException: ", err.Close.Detail, StringComparison.Ordinal);
            Assert.IsType<WebSocketException>(err.InnerException);
        }
        finally
        {
            await app.StopAsync();
        }
    }

    [Fact]
    public async Task AWebSocketSessionRecordsTheServerClose()
    {
        var (app, url) = await StartWsServer(async (_, ws) =>
        {
            await ws.CloseOutputAsync(WebSocketCloseStatus.PolicyViolation, "kicked", CancellationToken.None);
            try { await ws.ReceiveAsync(new byte[16], new CancellationTokenSource(Wait).Token); } catch { /* gone */ }
        });
        try
        {
            var session = Sessions.NewWsSession(url);
            await session.ConnectAsync();
            var deadline = DateTime.UtcNow + Wait;
            while (session.CloseInfo is null && DateTime.UtcNow < deadline)
            {
                await Task.Delay(10);
            }

            Assert.Equal(new TransportClose(CloseInitiator.Remote, 1008, "kicked"), session.CloseInfo);
            await session.CloseAsync();
            Assert.Equal(CloseInitiator.Remote, session.CloseInfo!.Initiator);
        }
        finally
        {
            await app.StopAsync();
        }
    }

    /// <summary>Accepts one telnet client, drains its negotiation, then runs <paramref name="end"/>.</summary>
    internal static async Task<(TelnetTransport Transport, Task Server)> ConnectTelnet(Action<Socket> end)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var server = Task.Run(async () =>
        {
            try
            {
                using var socket = await listener.AcceptSocketAsync();
                var buf = new byte[TelnetHandshakeBytes];
                var got = 0;
                while (got < buf.Length)
                {
                    var n = await socket.ReceiveAsync(buf.AsMemory(got), SocketFlags.None);
                    if (n == 0) break;
                    got += n;
                }

                end(socket);
            }
            finally
            {
                listener.Stop();
            }
        });
        var transport = new TelnetTransport();
        await transport.ConnectAsync("127.0.0.1", port);
        return (transport, server);
    }

    [Fact]
    public async Task TelnetEndOfStreamIsARemoteClose()
    {
        var (transport, server) = await ConnectTelnet(s =>
        {
            s.Shutdown(SocketShutdown.Both);
            s.Close();
        });
        await server;
        var err = await Assert.ThrowsAsync<TransportClosedException>(() => transport.ReceiveAsync(1024, Wait));
        Assert.Equal(new TransportClose(CloseInitiator.Remote), err.Close);
        Assert.Equal("connection closed by remote (remote close)", err.Message);
        Assert.False(transport.IsConnected());
    }

    [Fact]
    public async Task TelnetPeerResetIsARemoteClose()
    {
        var (transport, server) = await ConnectTelnet(s =>
        {
            // Zero linger turns close into an RST.
            s.LingerState = new LingerOption(true, 0);
            s.Close();
        });
        await server;
        var err = await Assert.ThrowsAsync<TransportClosedException>(() => transport.ReceiveAsync(1024, Wait));
        Assert.Equal(CloseInitiator.Remote, err.Close.Initiator);
        Assert.StartsWith("connection lost (remote close (SocketException: ", err.Message, StringComparison.Ordinal);
        Assert.False(transport.IsConnected());
    }
}
