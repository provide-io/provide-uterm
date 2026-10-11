//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using System.Threading.Channels;
using Provide.Uterm.Cli;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// <c>uterm proxy</c>: transport selection, the run loop's two ways to end, and
/// the browser/upstream bridge driven against an in-memory upstream.
/// </summary>
public sealed class Coverage100ProxyTests
{
    private class FakeTransport : IConnectionTransport
    {
        public Channel<byte[]> Inbound { get; } = Channel.CreateUnbounded<byte[]>();
        public Exception? ConnectError { get; init; }
        public Exception? SendError { get; init; }
        public Exception? DisconnectError { get; init; }
        public List<string> Sent { get; } = new();
        public TaskCompletionSource FirstSend { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int Disconnects { get; private set; }
        public string? ConnectedTo { get; private set; }

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            if (ConnectError is not null)
            {
                return Task.FromException(ConnectError);
            }

            ConnectedTo = $"{host}:{port}";
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            Disconnects++;
            return DisconnectError is null ? Task.CompletedTask : Task.FromException(DisconnectError);
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
        {
            if (SendError is not null)
            {
                return Task.FromException(SendError);
            }

            lock (Sent)
            {
                Sent.Add(Encoding.UTF8.GetString(data));
            }

            FirstSend.TrySetResult();
            return Task.CompletedTask;
        }

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default) =>
            await Inbound.Reader.ReadAsync(cancellationToken).ConfigureAwait(false);

        public bool IsConnected() => ConnectedTo is not null;
    }

    private sealed class AsyncDisposableTransport : FakeTransport, IAsyncDisposable
    {
        public bool Disposed { get; private set; }

        public ValueTask DisposeAsync()
        {
            Disposed = true;
            return ValueTask.CompletedTask;
        }
    }

    private sealed class DisposableTransport : FakeTransport, IDisposable
    {
        public bool Disposed { get; private set; }

        public void Dispose() => Disposed = true;
    }

    private static readonly ProxyCommand.Options Opts = new() { Host = "upstream.test", BbsPort = 2323 };

    private static async Task BridgeAsync(ScriptedWebSocket ws, FakeTransport tr)
    {
        var saved = ProxyCommand.TransportFactory;
        ProxyCommand.TransportFactory = _ => (tr, new ConnectOptions());
        try
        {
            await ProxyCommand.BridgeAsync(ws, Opts, CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));
        }
        finally
        {
            ProxyCommand.TransportFactory = saved;
        }
    }

    [Fact]
    public async Task Transport_selection_follows_the_transport_option()
    {
        var (ws, wsOpts) = ProxyCommand.CreateTransport(new ProxyCommand.Options { Transport = "WSS", Host = "h", BbsPort = 9 });
        Assert.IsType<WebSocketTransport>(ws);
        Assert.Equal("wss://h:9", wsOpts.Ws!.Url);
        Assert.False(wsOpts.Ws.SendBinary);
        await ((IAsyncDisposable)ws).DisposeAsync();

        var (ws2, ws2Opts) = ProxyCommand.CreateTransport(new ProxyCommand.Options { Transport = "ws", UpstreamWsUrl = "ws://up/x" });
        Assert.Equal("ws://up/x", ws2Opts.Ws!.Url);
        await ((IAsyncDisposable)ws2).DisposeAsync();

        var known = new List<string> { "/k" };
        var (ssh, sshOpts) = ProxyCommand.CreateTransport(new ProxyCommand.Options
        {
            Transport = "ssh", SshUser = "u", SshPassword = "p", KnownHostsFiles = known, InsecureSkipHostKeyVerify = true,
        });
        Assert.IsType<SshTransport>(ssh);
        Assert.Equal(("u", "p", true), (sshOpts.Ssh!.User, sshOpts.Ssh.Password, sshOpts.Ssh.InsecureSkipHostKeyVerify));
        Assert.Equal(known, sshOpts.Ssh.KnownHostsFiles);
        Assert.NotSame(known, sshOpts.Ssh.KnownHostsFiles); // copied, not aliased
        ((IDisposable)ssh).Dispose();

        var (telnet, telnetOpts) = ProxyCommand.CreateTransport(new ProxyCommand.Options { Transport = "anything-else" });
        Assert.IsType<TelnetTransport>(telnet);
        Assert.Equal("", telnetOpts.Ws.Url);
        Assert.Equal("", telnetOpts.Ssh.User);
        await ((IAsyncDisposable)telnet).DisposeAsync();
    }

    [Fact]
    public async Task Run_ends_quietly_whether_cancelled_before_or_after_start()
    {
        using (var pre = new CancellationTokenSource())
        {
            pre.Cancel();
            await ProxyCommand.RunAsync(new ProxyCommand.Options { Bind = "127.0.0.1", Port = C100Server.FreePort() }, pre.Token)
                .WaitAsync(TimeSpan.FromSeconds(10));
        }

        var port = C100Server.FreePort();
        using var cts = new CancellationTokenSource();
        var run = ProxyCommand.RunAsync(new ProxyCommand.Options { Bind = "127.0.0.1", Port = port }, cts.Token);
        using var http = new HttpClient();
        string? health = null;
        while (health is null)
        {
            Assert.False(run.IsCompleted, "the proxy stopped before it was cancelled");
            try
            {
                health = await http.GetStringAsync($"http://127.0.0.1:{port}/health");
            }
            catch (HttpRequestException)
            {
                await Task.Yield(); // not listening yet
            }
        }

        Assert.Contains("uterm-proxy", health, StringComparison.Ordinal);
        cts.Cancel();
        await run.WaitAsync(TimeSpan.FromSeconds(10));
        await Assert.ThrowsAsync<HttpRequestException>(() => http.GetStringAsync($"http://127.0.0.1:{port}/health"));
    }

    [Fact]
    public async Task Failed_upstream_connect_closes_an_open_browser_socket_only()
    {
        var open = new ScriptedWebSocket();
        await BridgeAsync(open, new FakeTransport { ConnectError = new IOException("refused") });
        Assert.Equal(WebSocketCloseStatus.InternalServerError, open.CloseStatus);

        var gone = new ScriptedWebSocket();
        gone.SetState(WebSocketState.Aborted);
        await BridgeAsync(gone, new FakeTransport { ConnectError = new IOException("refused") });
        Assert.Equal(0, gone.CloseCalls);
    }

    [Fact]
    public async Task Bridge_relays_both_ways_until_the_browser_closes()
    {
        var ws = new ScriptedWebSocket();
        var tr = new AsyncDisposableTransport();
        tr.Inbound.Writer.TryWrite(Array.Empty<byte>()); // an idle poll: nothing to forward
        tr.Inbound.Writer.TryWrite("from-upstream"u8.ToArray());
        ws.Enqueue(WebSocketMessageType.Text, Array.Empty<byte>()); // an empty frame: nothing to send
        ws.EnqueueText("from-browser");
        var bridge = BridgeAsync(ws, tr);
        await tr.FirstSend.Task.WaitAsync(TimeSpan.FromSeconds(10));
        while (ws.Sent.Count == 0)
        {
            await Task.Yield();
        }

        ws.EnqueueClose();
        await bridge;
        Assert.Equal("upstream.test:2323", tr.ConnectedTo);
        Assert.Equal(new[] { "from-browser" }, tr.Sent);
        var sent = Assert.Single(ws.Sent);
        Assert.Equal((WebSocketMessageType.Text, "from-upstream"), (sent.Type, Encoding.UTF8.GetString(sent.Data)));
        Assert.Equal(1, tr.Disconnects);
        Assert.True(tr.Disposed);
    }

    [Fact]
    public async Task An_upstream_send_failure_ends_the_bridge_and_a_failed_disconnect_is_ignored()
    {
        var ws = new ScriptedWebSocket();
        var tr = new DisposableTransport { SendError = new IOException("upstream gone"), DisconnectError = new IOException("already closed") };
        ws.EnqueueText("x");
        await BridgeAsync(ws, tr);
        Assert.Equal(1, tr.Disconnects);
        Assert.True(tr.Disposed);
    }

    [Fact]
    public async Task A_browser_send_failure_ends_the_bridge()
    {
        var ws = new ScriptedWebSocket { SendError = new WebSocketException("browser gone") };
        var tr = new FakeTransport();
        tr.Inbound.Writer.TryWrite("x"u8.ToArray());
        await BridgeAsync(ws, tr); // the browser pump is cancelled once the upstream pump fails
        Assert.Empty(ws.Sent);
        Assert.Equal(1, tr.Disconnects);
    }
}
