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
    internal sealed record ContractEvidence(
        int FragmentCount,
        int PreFinalActions,
        int PostFinalActions,
        bool OversizedRefused,
        List<string> DeliveredPayloads);

    internal static async Task<ContractEvidence> RunContractScenarioAsync(
        string endpoint,
        string payload,
        int fragmentCount,
        int oversizedBytes)
    {
        var fragments = System.Threading.Channels.Channel.CreateUnbounded<(string Endpoint, int Count, bool Final)>();
        var capture = endpoint == "browser" ? new CapturingWorker() : null;
        var fixture = await BootAsync(
            "contract-fragments-" + endpoint,
            capture,
            (kind, count, final) => fragments.Writer.TryWrite((kind, count, final)));
        await using var server = fixture.Server;
        using var browser = await ConnectBrowserAsync(fixture);
        using var producer = endpoint switch
        {
            "browser" => browser,
            "worker" => await ConnectWorkerEndpointAsync(fixture, "worker"),
            "tunnel" => await ConnectWorkerEndpointAsync(fixture, "tunnel"),
            _ => throw new ArgumentOutOfRangeException(nameof(endpoint)),
        };

        var wirePayload = endpoint == "tunnel"
            ? TunnelCodec.EncodeFrame(
                TunnelProtocol.ChannelData,
                Encoding.UTF8.GetBytes(payload),
                TunnelProtocol.FlagData)
            : Encoding.UTF8.GetBytes(payload);
        var beforeSeq = fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0;
        var preFinalActions = 0;
        for (var index = 0; index < fragmentCount; index++)
        {
            var start = wirePayload.Length * index / fragmentCount;
            var end = wirePayload.Length * (index + 1) / fragmentCount;
            var final = index == fragmentCount - 1;
            await producer.SendAsync(
                wirePayload.AsMemory(start, end - start),
                endpoint == "tunnel" ? WebSocketMessageType.Binary : WebSocketMessageType.Text,
                final,
                CancellationToken.None);
            using var fragmentTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var observed = await fragments.Reader.ReadAsync(fragmentTimeout.Token);
            Assert.Equal((endpoint, index + 1, final), observed);
            if (!final)
            {
                var acted = endpoint == "browser"
                    ? capture!.TryRead(out _)
                    : (fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0) != beforeSeq;
                if (acted) preFinalActions++;
            }
        }

        string delivered;
        if (endpoint == "browser")
        {
            delivered = await capture!.NextAsync();
        }
        else
        {
            delivered = await ReceiveUntilAsync(browser, payload);
            await WaitUntilAsync(() =>
                (fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0) == beforeSeq + 1);
        }
        Assert.Contains(payload, delivered, StringComparison.Ordinal);

        if (endpoint == "browser")
        {
            var ping = ControlChannelCodec.EncodeControlFrame(
                new Dictionary<string, object?> { ["type"] = "ping" });
            await browser.SendAsync(
                Encoding.UTF8.GetBytes(ping), WebSocketMessageType.Text, true, CancellationToken.None);
            Assert.Contains("\"type\":\"pong\"", await ReceiveTextAsync(browser));
            Assert.False(capture!.TryRead(out _));
        }
        else
        {
            var sentinel = "fragment-sentinel-" + endpoint;
            var sentinelPayload = endpoint == "tunnel"
                ? TunnelCodec.EncodeFrame(
                    TunnelProtocol.ChannelData,
                    Encoding.UTF8.GetBytes(sentinel),
                    TunnelProtocol.FlagData)
                : Encoding.UTF8.GetBytes(sentinel);
            await producer.SendAsync(
                sentinelPayload,
                endpoint == "tunnel" ? WebSocketMessageType.Binary : WebSocketMessageType.Text,
                true,
                CancellationToken.None);
            Assert.Contains(sentinel, await ReceiveUntilAsync(browser, sentinel));
            await WaitUntilAsync(() =>
                (fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0) == beforeSeq + 2);
        }

        var deliveredSeq = fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0;
        await producer.SendAsync(
            new byte[oversizedBytes],
            endpoint == "browser" ? WebSocketMessageType.Text : WebSocketMessageType.Binary,
            true,
            CancellationToken.None);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var close = await WebSocketMessageReader.ReadAsync(producer, oversizedBytes + 1024, timeout.Token);
        Assert.True(close.IsClose);
        Assert.Equal(WebSocketCloseStatus.MessageTooBig, close.CloseStatus);
        var oversizedActed = endpoint == "browser"
            ? capture!.TryRead(out _)
            : (fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0) != deliveredSeq;
        Assert.False(oversizedActed);

        var postFinalActions = endpoint == "browser"
            ? 1
            : (fixture.Hub.Registry.Get(fixture.WorkerId)?.EventSeq ?? 0) - beforeSeq - 1;
        return new ContractEvidence(fragmentCount, preFinalActions, postFinalActions, true, [payload]);
    }

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

        var unicodePing = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
            new Dictionary<string, object?> { ["type"] = "ping", ["label"] = "café 東京" }));
        await SendFragmentedAsync(browser, unicodePing, WebSocketMessageType.Binary);
        Assert.Contains("\"type\":\"pong\"", await ReceiveTextAsync(browser));

        await SendFragmentedAsync(browser, Encoding.UTF8.GetBytes("typed"), WebSocketMessageType.Text);
        Assert.Equal("typed", await capture.NextAsync());

        byte[] rawInput = [0xff, 0x80, (byte)'A'];
        await SendFragmentedAsync(browser, rawInput, WebSocketMessageType.Binary);
        Assert.Equal(rawInput, Encoding.Latin1.GetBytes(await capture.NextAsync()));
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

        var unicodeSnapshot = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
            new Dictionary<string, object?> { ["type"] = "snapshot", ["text"] = "café 東京" }));
        await SendFragmentedAsync(worker, unicodeSnapshot, WebSocketMessageType.Binary);
        await WaitUntilAsync(() =>
            fixture.Hub.Registry.Get(fixture.WorkerId)?.LastSnapshot?["text"]?.ToString() == "café 東京");

        await SendFragmentedAsync(worker, Encoding.UTF8.GetBytes("terminal"), WebSocketMessageType.Text);
        Assert.Contains("terminal", await ReceiveUntilAsync(browser, "terminal"));

        byte[] rawTerminal = [0xff, 0x80, (byte)'B'];
        await SendFragmentedAsync(worker, rawTerminal, WebSocketMessageType.Binary);
        var rawTermMessage = await ReceiveUntilAsync(browser, "ÿ");
        Assert.Contains("ÿ", rawTermMessage);
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

    private static async Task<Fixture> BootAsync(
        string workerId,
        IWorkerWs? existingWorker = null,
        Action<string, int, bool>? fragmentObserved = null)
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
            WebSocketFragmentObserved = fragmentObserved,
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

        public bool TryRead(out string payload) => _messages.Reader.TryRead(out payload!);
    }
}
