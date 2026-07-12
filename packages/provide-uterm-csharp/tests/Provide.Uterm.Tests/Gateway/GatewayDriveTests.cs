//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using Provide.Uterm.Cli;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Gateway;

namespace Provide.Uterm.Tests.Gateway;

public class GatewayDriveTests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    [Fact]
    public void TelnetWriteTransform_Crlf_And_Del()
    {
        var raw = Encoding.ASCII.GetBytes("hi\nthere\x7f");
        var got = GatewayDrive.TelnetWriteTransform(raw);
        var s = Encoding.GetEncoding("ISO-8859-1").GetString(got);
        Assert.Contains("\r\n", s);
        Assert.Contains('\b', s); // DEL was rewritten to BS
    }

    [Fact]
    public async Task Drive_TcpToWs_And_Back_WithLocalEcho()
    {
        // Local WS echo server that sends a control frame then a prompt, and echoes client data as terminal text.
        var wsPort = FreePort();
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel().UseUrls($"http://127.0.0.1:{wsPort}");
        var app = builder.Build();
        app.UseWebSockets();
        app.Map("/ws/terminal", async ctx =>
        {
            if (!ctx.WebSockets.IsWebSocketRequest)
            {
                ctx.Response.StatusCode = 400;
                return;
            }

            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            // hello from client expected; send control + terminal
            var buf = new byte[4096];
            _ = await ws.ReceiveAsync(buf, CancellationToken.None); // hello control
            var ctrl = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "render_speed",
                ["cps"] = 2400,
            });
            await ws.SendAsync(Encoding.UTF8.GetBytes(ctrl), WebSocketMessageType.Text, true, CancellationToken.None);
            await ws.SendAsync(Encoding.UTF8.GetBytes("What is your name? "), WebSocketMessageType.Text, true, CancellationToken.None);

            // echo one client message
            var r = await ws.ReceiveAsync(buf, CancellationToken.None);
            if (r.Count > 0)
            {
                var text = Encoding.UTF8.GetString(buf, 0, r.Count);
                // may be DLE-escaped terminal data — just echo a fixed ack
                await ws.SendAsync(Encoding.UTF8.GetBytes("ok\n"), WebSocketMessageType.Text, true, CancellationToken.None);
            }

            await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", CancellationToken.None);
        });
        await app.StartAsync();
        try
        {
            var serverPort = FreePort();
            var tcpListen = new TcpListener(IPAddress.Loopback, serverPort);
            tcpListen.Start();
            var acceptTask = Task.Run(async () =>
            {
                var c = await tcpListen.AcceptTcpClientAsync();
                await GatewayDrive.RunAsync(c, $"ws://127.0.0.1:{wsPort}/ws/terminal");
            });

            using var tcp = new TcpClient();
            await tcp.ConnectAsync(IPAddress.Loopback, serverPort);
            await using var stream = tcp.GetStream();
            var readBuf = new byte[4096];
            using var readCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var n = await stream.ReadAsync(readBuf.AsMemory(0, readBuf.Length), readCts.Token);
            var text = Encoding.UTF8.GetString(readBuf, 0, n);
            Assert.Contains("What is your name?", text, StringComparison.Ordinal);

            // send name
            var name = Encoding.ASCII.GetBytes("alice\r\n");
            await stream.WriteAsync(name);
            await stream.FlushAsync();

            // read ack
            n = await stream.ReadAsync(readBuf.AsMemory(0, readBuf.Length), readCts.Token);
            var ack = Encoding.UTF8.GetString(readBuf, 0, n);
            Assert.Contains("ok", ack, StringComparison.Ordinal);

            tcpListen.Stop();
            try { await acceptTask; } catch { /* cancelled */ }
        }
        finally
        {
            await app.StopAsync();
        }
    }

    [Fact]
    public async Task Drive_Forwards_Binary_And_MalformedControl_AsRawBytes()
    {
        var wsPort = FreePort();
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel().UseUrls($"http://127.0.0.1:{wsPort}");
        var app = builder.Build();
        app.UseWebSockets();
        app.Map("/ws/terminal", async ctx =>
        {
            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            var hello = new byte[4096];
            _ = await ws.ReceiveAsync(hello, CancellationToken.None);
            await ws.SendAsync(new byte[] { 0x01, 0x02, 0x03 }, WebSocketMessageType.Binary, true, CancellationToken.None);
            await ws.SendAsync(Encoding.UTF8.GetBytes("\u0010\u000200000008:not-json"), WebSocketMessageType.Text, true, CancellationToken.None);
            await Task.Delay(100);
            await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", CancellationToken.None);
        });
        await app.StartAsync();
        try
        {
            using var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            var port = ((IPEndPoint)listener.LocalEndpoint).Port;
            var drive = Task.Run(async () =>
            {
                var accepted = await listener.AcceptTcpClientAsync();
                await GatewayDrive.RunAsync(accepted, $"ws://127.0.0.1:{wsPort}/ws/terminal");
            });

            using var tcp = new TcpClient();
            await tcp.ConnectAsync(IPAddress.Loopback, port);
            await using var stream = tcp.GetStream();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            using var received = new MemoryStream();
            var buffer = new byte[256];
            while (received.Length < 3 + "\u0010\u000200000008:not-json".Length)
            {
                var count = await stream.ReadAsync(buffer, timeout.Token);
                if (count == 0)
                {
                    break;
                }
                received.Write(buffer, 0, count);
            }

            var payload = received.ToArray();
            Assert.Equal(new byte[] { 0x01, 0x02, 0x03 }, payload[..3]);
            Assert.Equal("\u0010\u000200000008:not-json", Encoding.UTF8.GetString(payload[3..]));
            await drive;
        }
        finally
        {
            await app.StopAsync();
        }
    }

    [Fact]
    public void Listen_Cli_RequiresWsUrl_And_Once()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(new[] { "listen", "--once" }, o, e));
        Assert.Contains("WS_URL", e.ToString(), StringComparison.Ordinal);

        var port = FreePort();
        e.GetStringBuilder().Clear();
        o.GetStringBuilder().Clear();
        Assert.Equal(0, Root.Execute(
            new[] { "listen", "wss://example.com/ws/terminal", "--host", "127.0.0.1", "--port", port.ToString(), "--once" },
            o, e));
        Assert.Contains("listen ready", o.ToString(), StringComparison.Ordinal);
        Assert.Contains("wss://example.com/ws/terminal", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Proxy_Cli_WebSocket_Validates_And_Accepts_PositionalUrl()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();

        Assert.Equal(1, Root.Execute(
            new[] { "proxy", "--transport", "websocket", "--once" }, o, e));
        Assert.Contains("websocket proxy requires --url", e.ToString(), StringComparison.Ordinal);

        o.GetStringBuilder().Clear();
        e.GetStringBuilder().Clear();
        var port = FreePort();
        Assert.Equal(0, Root.Execute(
            new[]
            {
                "proxy", "wss://example.com/ws/terminal", "--transport", "websocket",
                "--port", port.ToString(), "--once",
            },
            o,
            e));
        Assert.Contains("wss://example.com/ws/terminal", o.ToString(), StringComparison.Ordinal);
        Assert.Contains("proxy health:", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Listen_Cli_Help_UrlFlag_And_SshRejection()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();

        Assert.Equal(0, Root.Execute(new[] { "listen", "--help" }, o, e));
        Assert.Contains("uterm listen WS_URL", o.ToString(), StringComparison.Ordinal);

        o.GetStringBuilder().Clear();
        e.GetStringBuilder().Clear();
        var port = FreePort();
        Assert.Equal(0, Root.Execute(
            new[]
            {
                "listen", "--url", "ws://example.com/ws/terminal", "--port", port.ToString(), "--once",
            },
            o,
            e));
        Assert.Contains("listen ready", o.ToString(), StringComparison.Ordinal);

        o.GetStringBuilder().Clear();
        e.GetStringBuilder().Clear();
        o.GetStringBuilder().Clear();
        e.GetStringBuilder().Clear();
        var sshPort = FreePort();
        Assert.Equal(0, Root.Execute(
            new[]
            {
                "listen", "wss://example.com/ws/terminal", "--protocol", "ssh",
                "--host", "127.0.0.1", "--port", sshPort.ToString(), "--once",
            },
            o,
            e));
        Assert.Contains("listen: ssh gateway", o.ToString(), StringComparison.Ordinal);
        Assert.Contains("listen ready", o.ToString(), StringComparison.Ordinal);
    }
}
