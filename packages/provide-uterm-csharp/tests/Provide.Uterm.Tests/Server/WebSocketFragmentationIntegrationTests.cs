//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests.Server;

public sealed class WebSocketFragmentationIntegrationTests
{
    [Fact]
    public async Task BrowserLoop_ReassemblesFragmentedControlAndInput()
    {
        var capture = new CapturingWorker();
        var fixture = await BootAsync("browser-fragments", capture);
        await using var server = fixture.Server;
        using var browser = await ConnectBrowserAsync(fixture);

        var ping = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
            new Dictionary<string, object?> { ["type"] = "ping" }));
        await SendFragmentedAsync(browser, ping, WebSocketMessageType.Text);
        Assert.Contains("\"type\":\"pong\"", await ReceiveTextAsync(browser));

        await SendFragmentedAsync(browser, Encoding.UTF8.GetBytes("typed"), WebSocketMessageType.Text);
        Assert.Equal("typed", await capture.NextAsync());
    }

    [Fact]
    public async Task WorkerLoop_ReassemblesFragmentedSnapshotAndTerminalData()
    {
        var fixture = await BootAsync("worker-fragments");
        await using var server = fixture.Server;
        using var browser = await ConnectBrowserAsync(fixture);
        using var worker = new ClientWebSocket();
        worker.Options.SetRequestHeader("Authorization", "Bearer " + fixture.WorkerToken);
        await worker.ConnectAsync(fixture.WorkerUri, CancellationToken.None);

        var snapshot = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
            new Dictionary<string, object?> { ["type"] = "snapshot", ["text"] = "whole" }));
        await SendFragmentedAsync(worker, snapshot, WebSocketMessageType.Text);
        await WaitUntilAsync(() => fixture.Hub.Registry.Get(fixture.WorkerId)?.LastSnapshot is not null);
        Assert.Equal("whole", fixture.Hub.Registry.Get(fixture.WorkerId)!.LastSnapshot!["text"]?.ToString());

        await SendFragmentedAsync(worker, Encoding.UTF8.GetBytes("terminal"), WebSocketMessageType.Text);
        Assert.Contains("terminal", await ReceiveUntilAsync(browser, "terminal"));
    }

    [Fact]
    public async Task TunnelLoop_ReassemblesFragmentedBinaryFrame()
    {
        var fixture = await BootAsync("tunnel-fragments");
        await using var server = fixture.Server;
        using var browser = await ConnectBrowserAsync(fixture);
        using var tunnel = new ClientWebSocket();
        tunnel.Options.SetRequestHeader("Authorization", "Bearer " + fixture.WorkerToken);
        await tunnel.ConnectAsync(fixture.TunnelUri, CancellationToken.None);

        var frame = TunnelCodec.EncodeFrame(
            TunnelProtocol.ChannelData,
            Encoding.UTF8.GetBytes("tunnel-data"),
            TunnelProtocol.FlagData);
        await SendFragmentedAsync(tunnel, frame, WebSocketMessageType.Binary);

        Assert.Contains("tunnel-data", await ReceiveUntilAsync(browser, "tunnel-data"));
    }

    [Theory]
    [InlineData("browser")]
    [InlineData("worker")]
    [InlineData("tunnel")]
    public async Task ReceiveLoopsAcknowledgePeerClose(string endpoint)
    {
        var fixture = await BootAsync("close-ack-" + endpoint);
        await using var server = fixture.Server;
        using var socket = endpoint == "browser"
            ? await ConnectBrowserAsync(fixture)
            : await ConnectWorkerEndpointAsync(fixture, endpoint);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));

        await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "bye", timeout.Token);
        var response = await WebSocketMessageReader.ReadAsync(socket, 1024, timeout.Token);

        Assert.True(response.IsClose);
        Assert.Equal(WebSocketCloseStatus.NormalClosure, response.CloseStatus);
    }

    private static async Task SendFragmentedAsync(
        ClientWebSocket socket, byte[] payload, WebSocketMessageType messageType)
    {
        var split = Math.Max(1, payload.Length / 2);
        await socket.SendAsync(payload.AsMemory(0, split), messageType, false, CancellationToken.None);
        await socket.SendAsync(payload.AsMemory(split), messageType, true, CancellationToken.None);
    }

    private static async Task<string> ReceiveUntilAsync(ClientWebSocket socket, string expected)
    {
        for (var i = 0; i < 8; i++)
        {
            var value = await ReceiveTextAsync(socket);
            if (value.Contains(expected, StringComparison.Ordinal)) return value;
        }

        throw new Xunit.Sdk.XunitException($"Did not receive payload containing {expected}.");
    }

    private static async Task<string> ReceiveTextAsync(ClientWebSocket socket)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var message = await WebSocketMessageReader.ReadAsync(socket, 1_048_576, timeout.Token);
        return Encoding.UTF8.GetString(message.Payload);
    }

    private static async Task<ClientWebSocket> ConnectBrowserAsync(Fixture fixture)
    {
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer " + fixture.BrowserToken);
        await socket.ConnectAsync(fixture.BrowserUri, CancellationToken.None);
        for (var i = 0; i < 3; i++) await ReceiveTextAsync(socket);
        return socket;
    }

    private static async Task<ClientWebSocket> ConnectWorkerEndpointAsync(Fixture fixture, string endpoint)
    {
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer " + fixture.WorkerToken);
        await socket.ConnectAsync(
            endpoint == "worker" ? fixture.WorkerUri : fixture.TunnelUri,
            CancellationToken.None);
        return socket;
    }

    private static async Task WaitUntilAsync(Func<bool> predicate)
    {
        for (var i = 0; i < 100 && !predicate(); i++) await Task.Delay(10);
        Assert.True(predicate());
    }

    private static async Task<Fixture> BootAsync(string workerId, IWorkerWs? existingWorker = null)
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
        cfg.Auth.WorkerBearerToken = "worker-secret";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = workerId,
            DisplayName = workerId,
            Visibility = "public",
            Owner = "admin",
            InputMode = "open",
            AutoStart = false,
        });
        var browserToken = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "wsfrag-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            WorkerToken = cfg.Auth.WorkerBearerToken,
            MaxWsMessageBytes = 1_048_576,
        });
        if (existingWorker is not null)
        {
            hub.Conn.RegisterWorker(workerId, existingWorker);
            hub.Registry.Get(workerId)!.InputMode = InputModes.Open;
        }
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        var wsBase = $"ws://127.0.0.1:{port}";
        return new Fixture(
            server,
            hub,
            workerId,
            browserToken,
            cfg.Auth.WorkerBearerToken,
            new Uri($"{wsBase}/ws/browser/{workerId}/term"),
            new Uri($"{wsBase}/ws/worker/{workerId}/term"),
            new Uri($"{wsBase}/tunnel/{workerId}"));
    }

    private sealed record Fixture(
        UtermServer Server,
        TermHub Hub,
        string WorkerId,
        string BrowserToken,
        string WorkerToken,
        Uri BrowserUri,
        Uri WorkerUri,
        Uri TunnelUri);

    private sealed class CapturingWorker : IWorkerWs
    {
        private readonly System.Threading.Channels.Channel<string> _messages =
            System.Threading.Channels.Channel.CreateUnbounded<string>();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            _messages.Writer.WriteAsync(payload, cancellationToken).AsTask();

        public async Task<string> NextAsync()
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            return await _messages.Reader.ReadAsync(timeout.Token);
        }
    }
}
