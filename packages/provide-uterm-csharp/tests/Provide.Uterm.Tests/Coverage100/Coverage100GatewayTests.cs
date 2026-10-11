//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Gateway;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The gateway accept loops and the telnet/SSH ↔ WebSocket pump, driven over a
/// scripted WebSocket and duplex stream so every exit path is deterministic.
/// </summary>
public sealed class Coverage100GatewayTests
{
    private static async Task WithSocketAsync(ScriptedWebSocket ws, Func<Task> body)
    {
        var saved = GatewayDrive.ConnectWebSocketAsync;
        GatewayDrive.ConnectWebSocketAsync = (_, _) => Task.FromResult<WebSocket>(ws);
        try
        {
            await body();
        }
        finally
        {
            GatewayDrive.ConnectWebSocketAsync = saved;
        }
    }

    [Fact]
    public async Task Local_eof_ends_the_session_and_closes_the_socket_gracefully()
    {
        var ws = new ScriptedWebSocket();
        var local = new ScriptedDuplexStream();
        local.EnqueueRead("hi"u8.ToArray());
        await WithSocketAsync(ws, async () =>
        {
            var run = GatewayDrive.RunAsync(local, "ws://scripted/ws");
            // The terminal bytes reach the socket before EOF ends the session.
            await WaitForAsync(() => ws.Sent.Count >= 2);
            local.EnqueueEof();
            await run.WaitAsync(TimeSpan.FromSeconds(10));
        });

        var sent = ws.Sent.Select(m => Encoding.UTF8.GetString(m.Data)).ToList();
        Assert.True(ControlChannelCodec.IsControlFrame(sent[0])); // the hello
        Assert.Equal(ControlChannelCodec.EncodeTerminalData("hi"), sent[1]);
        Assert.Equal(1, ws.CloseCalls);
        Assert.Equal(WebSocketCloseStatus.NormalClosure, ws.CloseStatus);
    }

    [Fact]
    public async Task A_close_that_fails_is_ignored()
    {
        var ws = new ScriptedWebSocket { CloseError = new WebSocketException("gone") };
        var local = new ScriptedDuplexStream();
        local.EnqueueEof();
        await WithSocketAsync(ws, () => GatewayDrive.RunAsync(local, "ws://scripted/ws").WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(1, ws.CloseCalls);
    }

    [Fact]
    public async Task Socket_messages_reach_the_local_side_until_the_peer_closes()
    {
        var ws = new ScriptedWebSocket();
        var local = new ScriptedDuplexStream();
        ws.Enqueue(WebSocketMessageType.Binary, "B\x7f"u8.ToArray());
        ws.Enqueue(WebSocketMessageType.Text, Array.Empty<byte>());
        ws.EnqueueText(ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "session_token",
            ["token"] = "t",
        }) + ControlChannelCodec.EncodeTerminalData("x\n"));
        // Undecodable control frame: written through raw.
        var bad = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["k"] = 1 });
        ws.EnqueueText(bad[..^2] + "!}");
        ws.EnqueueClose();

        await WithSocketAsync(ws, () => GatewayDrive.RunAsync(local, "ws://scripted/ws").WaitAsync(TimeSpan.FromSeconds(10)));

        var written = Encoding.Latin1.GetString(local.Written);
        Assert.StartsWith("B\x7f", written, StringComparison.Ordinal); // binary passes untransformed
        Assert.Contains("x\r\n", written, StringComparison.Ordinal);
        Assert.EndsWith("!}", written, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_local_write_failure_ends_the_session()
    {
        var ws = new ScriptedWebSocket();
        var local = new ScriptedDuplexStream { WriteError = new IOException("local gone") };
        ws.Enqueue(WebSocketMessageType.Binary, "B"u8.ToArray());
        await WithSocketAsync(ws, () => GatewayDrive.RunAsync(local, "ws://scripted/ws").WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Empty(local.Written);
    }

    [Fact]
    public async Task Connect_failure_to_a_closed_port_propagates()
    {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        var port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        await Assert.ThrowsAnyAsync<Exception>(() =>
            GatewayDrive.RunAsync(new ScriptedDuplexStream(), $"ws://127.0.0.1:{port}/ws"));
    }

    [Fact]
    public void Pump_reads_player_ids_of_every_numeric_shape_and_rejects_others()
    {
        long? Pid(object? v)
        {
            var st = new GatewayPump.ControlState();
            Assert.True(GatewayPump.HandleControlFrame(
                new Dictionary<string, object?> { ["type"] = "session_token", ["token"] = "t", ["player_id"] = v }, st));
            return st.Token!.PlayerId;
        }

        Assert.Equal(7L, Pid(7L));
        Assert.Equal(7L, Pid(7));
        Assert.Equal(7L, Pid(7.9));
        Assert.Equal(7L, Pid("7"));
        Assert.Null(Pid("seven"));
        Assert.False(GatewayBindPolicy.IsLoopbackBindHost(""));
    }

    [Fact]
    public async Task Accept_loops_close_unhandled_clients_and_clients_whose_handler_throws()
    {
        await using var telnet = new TelnetGateway();
        await telnet.StartAsync("127.0.0.1", FreePort());
        await AssertClosedByServerAsync(telnet.Port);

        var handled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        telnet.OnAccept = (_, _) =>
        {
            handled.TrySetResult();
            throw new InvalidOperationException("handler boom");
        };
        await AssertClosedByServerAsync(telnet.Port);
        await handled.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await telnet.StopAsync();
        await telnet.StopAsync(); // idempotent

        await using var ssh = new SshGateway();
        await ssh.StopAsync(); // never started
        await ssh.StartAsync("127.0.0.1", FreePort());
        await AssertClosedByServerAsync(ssh.Port);
        var ok = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        ssh.OnAccept = (c, _) =>
        {
            c.Dispose();
            ok.TrySetResult();
            return Task.CompletedTask;
        };
        await AssertClosedByServerAsync(ssh.Port);
        await ok.Task.WaitAsync(TimeSpan.FromSeconds(10));
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static async Task AssertClosedByServerAsync(int port)
    {
        using var c = new TcpClient();
        await c.ConnectAsync(IPAddress.Loopback, port);
        var buf = new byte[1];
        int n;
        try
        {
            n = await c.GetStream().ReadAsync(buf).AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        }
        catch (IOException)
        {
            n = 0; // reset rather than FIN: still a server-side close
        }

        Assert.Equal(0, n);
    }

    private static async Task WaitForAsync(Func<bool> cond)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (!cond())
        {
            Assert.True(DateTime.UtcNow < deadline, "condition not reached");
            await Task.Delay(1);
        }
    }
}
