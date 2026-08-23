//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using Provide.Uterm.Cli;
using Provide.Uterm.Client;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;
using Provide.Uterm.Hub;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests;

public class TermSessionCliServerTests
{
    private sealed class FakeTransport : IConnectionTransport
    {
        private readonly Queue<byte[]> _incoming = new();
        private bool _connected;
        public List<byte[]> Sent { get; } = new();

        public void Enqueue(string text) => _incoming.Enqueue(Encoding.UTF8.GetBytes(text));

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _connected = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _connected = false;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
        {
            Sent.Add(data);
            return Task.CompletedTask;
        }

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            var deadline = DateTime.UtcNow + timeout;
            while (DateTime.UtcNow < deadline)
            {
                cancellationToken.ThrowIfCancellationRequested();
                lock (_incoming)
                {
                    if (_incoming.Count > 0)
                    {
                        return _incoming.Dequeue();
                    }
                }

                await Task.Delay(5, cancellationToken).ConfigureAwait(false);
            }

            return Array.Empty<byte>();
        }

        public bool IsConnected() => _connected;
    }

    [Fact]
    public async Task TransportSession_Connect_Send_Process_Close()
    {
        var transport = new FakeTransport();
        await using var session = new TransportSession(
            transport,
            ct => transport.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { Cols = 40, Rows = 10, ControlFrames = true });

        Assert.False(session.IsConnected());
        await session.ConnectAsync();
        Assert.True(session.IsConnected());

        transport.Enqueue("Hello from fake\r\n");
        // Allow reader loop to process
        var saw = false;
        for (var i = 0; i < 50; i++)
        {
            if (session.Emulator().GetSnapshot().Screen.Contains("Hello", StringComparison.Ordinal))
            {
                saw = true;
                break;
            }

            await Task.Delay(20);
        }

        Assert.True(saw);
        Assert.True(session.UpdateSeq() >= 0);
        Assert.True(session.ScreenChangeSeq() >= 0);

        await session.SendAsync("ping");
        Assert.Contains(transport.Sent, b => Encoding.UTF8.GetString(b) == "ping");

        await session.CloseAsync();
        Assert.False(session.IsConnected());
    }

    [Fact]
    public void TransportSession_FactoryMethods()
    {
        var t1 = TransportSession.ConnectTelnet("127.0.0.1", 1);
        Assert.NotNull(t1);
        var t2 = TransportSession.ConnectWS("ws://127.0.0.1:1");
        Assert.NotNull(t2);
        var t3 = Sessions.NewTelnetSession("127.0.0.1", 1, new TelnetOptions { Cols = 80, Rows = 25 });
        Assert.NotNull(t3);
        var t4 = Sessions.NewWsSession("ws://x", new WsOptions { Url = "ws://x" });
        Assert.NotNull(t4);
        var t5 = Sessions.NewWSSession("ws://y");
        Assert.NotNull(t5);

        var opts = new TelnetOptions { Cols = 10, Rows = 5, Term = "vt100", Timeout = TimeSpan.FromSeconds(1) };
        var co = opts.ToConnectOptions();
        Assert.Equal(10, co.Cols);
        Assert.Equal(5, co.Rows);
    }

    [Fact]
    public void ConnectOptions_WithDefaults()
    {
        var o = new ConnectOptions().WithDefaults();
        Assert.Equal(TransportDefaults.DefaultCols, o.Cols);
        Assert.Equal(TransportDefaults.DefaultRows, o.Rows);
        Assert.Equal(TransportDefaults.DefaultTerm, o.Term);
        Assert.True(o.Timeout > TimeSpan.Zero);
    }

    [Fact]
    public async Task Cli_EachSubcommand_Help()
    {
        foreach (var cmd in Root.Subcommands)
        {
            using var sw = new StringWriter();
            using var err = new StringWriter();
            var code = Root.Execute(new[] { cmd, "--help" }, sw, err);
            Assert.Equal(0, code);
            Assert.Contains(cmd, sw.ToString(), StringComparison.OrdinalIgnoreCase);
        }

        using var o = new StringWriter();
        Assert.Equal(0, Root.Execute(new[] { "--version" }, o, o));
        Assert.Contains(Root.Version, o.ToString(), StringComparison.Ordinal);

        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(new[] { "nope" }, new StringWriter(), e));
        Assert.Contains("unknown", e.ToString(), StringComparison.Ordinal);

        // Real subcommands with --once / help / required args (no long-running block).
        using (var sw = new StringWriter())
        {
            Assert.Equal(0, Root.Execute(new[] { "proxy", "127.0.0.1", "23", "--port", "18700", "--once", "--bind", "127.0.0.1" }, sw, sw));
            Assert.DoesNotContain("stub", sw.ToString(), StringComparison.OrdinalIgnoreCase);
        }

        using (var sw = new StringWriter())
        {
            Assert.Equal(0, Root.Execute(new[] { "listen", "ws://127.0.0.1:9/ws", "--once", "--host", "127.0.0.1", "--port", FreePort().ToString() }, sw, sw));
        }

        using (var sw = new StringWriter())
        {
            // Portable no-op process: Unix `true`, Windows `cmd /c exit 0`.
            var noop = OperatingSystem.IsWindows() ? "cmd /c exit 0" : "true";
            Assert.Equal(0, Root.Execute(new[] { "share", "--once", "--command", noop }, sw, sw));
        }

        using (var sw = new StringWriter())
        {
            // missing required --url → non-zero
            Assert.Equal(1, Root.Execute(new[] { "tunnel" }, sw, sw));
        }

        using (var sw = new StringWriter())
        {
            Assert.Equal(1, Root.Execute(new[] { "inspect" }, sw, sw)); // missing --upstream
        }

        using (var sw = new StringWriter())
        {
            // watch against down server may fail; help is covered above
            Assert.Equal(0, Root.Execute(new[] { "watch", "--help" }, sw, sw));
        }

        using (var sw = new StringWriter())
        {
            Assert.Equal(1, Root.Execute(new[] { "audit" }, sw, sw)); // needs verify PATH
        }

        Assert.Equal(0, await Root.ExecuteAsync(new[] { "--help" }));
    }

    [Fact]
    public async Task Server_CreateHandler_And_Factory_HealthSessions()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "s1",
            DisplayName = "S1",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "dev",
        });

        var (server, token) = ServerFactory.CreateFromConfig(cfg, "test");
        Assert.False(string.IsNullOrEmpty(token));
        // Register worker so hijack surface is live for the session id
        // ServerFactory already built a hub; re-create with known hub for worker reg.
        await using (server)
        {
            // Use CreateHandler path: build + handler forces pipeline start
            server.Build(new[] { $"http://127.0.0.1:{port}" });
            using var _ = server.CreateHandler();
            await server.StartAsync();
            Assert.False(string.IsNullOrEmpty(server.BaseAddress));

            using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
            var healthz = await http.GetAsync("/healthz");
            healthz.EnsureSuccessStatusCode();

            using var client = HijackClient.WithBearer(server.BaseAddress!, token!);
            var health = await client.HealthAsync();
            Assert.Equal("ok", health["status"]?.ToString());
            var sessions = await client.ListSessionsAsync();
            var json = System.Text.Json.JsonSerializer.Serialize(sessions);
            Assert.Contains("s1", json, StringComparison.Ordinal);
        }
    }


    [Fact]
    public void Tunnel_AllChannels_RoundTrip()
    {
        foreach (var ch in new byte[]
                 {
                     TunnelProtocol.ChannelControl,
                     TunnelProtocol.ChannelData,
                     TunnelProtocol.ChannelTcp,
                     TunnelProtocol.ChannelHttp,
                 })
        {
            var payload = Encoding.UTF8.GetBytes("p-" + ch);
            var frame = TunnelCodec.EncodeFrame(ch, payload);
            var decoded = TunnelCodec.DecodeFrame(frame);
            Assert.Equal(ch, decoded.Channel);
            Assert.Equal(payload, decoded.Payload);
        }

        var ctrl = TunnelCodec.EncodeControlBytes(Encoding.UTF8.GetBytes("""{"type":"ping"}"""));
        Assert.Equal(TunnelProtocol.ChannelControl, ctrl[0]);
    }

    [Fact]
    public void Filters_EofAndBinaryReaderOverloads()
    {
        using var empty = new MemoryStream(Array.Empty<byte>());
        Provide.Uterm.Filters.InputFilters.ConsumeIac(empty);
        Provide.Uterm.Filters.InputFilters.ConsumeEscape(empty);

        var data = new byte[] { Provide.Uterm.Filters.InputFilters.Will, 1, (byte)'Z' };
        using var ms = new MemoryStream(data);
        using var br = new BinaryReader(ms);
        Provide.Uterm.Filters.InputFilters.ConsumeIac(br);
        Assert.Equal((byte)'Z', br.ReadByte());

        var esc = new byte[] { (byte)'[', (byte)'A', (byte)'Q' };
        using var ms2 = new MemoryStream(esc);
        using var br2 = new BinaryReader(ms2);
        Provide.Uterm.Filters.InputFilters.ConsumeEscape(br2);
        Assert.Equal((byte)'Q', br2.ReadByte());
    }


    private static int FreePort()
    {
        var probe = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        probe.Start();
        var p = ((System.Net.IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        return p;
    }
}
