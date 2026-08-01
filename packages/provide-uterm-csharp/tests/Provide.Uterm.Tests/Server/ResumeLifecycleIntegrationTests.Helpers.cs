//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Reflection;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    private static async Task<Dictionary<string, object?>> DrainHandshakeAsync(ClientWebSocket socket)
    {
        Dictionary<string, object?>? hello = null;
        for (var i = 0; i < 3; i++)
        {
            var frame = await ReceiveFrameAsync(socket);
            if (Type(frame) == "hello") hello = frame;
        }

        return hello ?? throw new Xunit.Sdk.XunitException("handshake did not contain hello");
    }

    private static async Task SendControlAsync(ClientWebSocket socket, string type, string? token = null)
    {
        var frame = new Dictionary<string, object?> { ["type"] = type };
        if (token is not null) frame["token"] = token;
        var bytes = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(frame));
        await socket.SendAsync(bytes, WebSocketMessageType.Text, true, CancellationToken.None);
    }

    private static async Task<Dictionary<string, object?>> ReceiveUntilAsync(
        ClientWebSocket socket,
        Func<Dictionary<string, object?>, bool> predicate)
    {
        for (var i = 0; i < 12; i++)
        {
            var frame = await ReceiveFrameAsync(socket);
            if (predicate(frame)) return frame;
        }

        throw new Xunit.Sdk.XunitException("expected control frame was not received");
    }

    private static async Task<List<Dictionary<string, object?>>> ReceiveThroughAsync(
        ClientWebSocket socket,
        Func<Dictionary<string, object?>, bool> predicate)
    {
        var frames = new List<Dictionary<string, object?>>();
        for (var i = 0; i < 12; i++)
        {
            var frame = await ReceiveFrameAsync(socket);
            frames.Add(frame);
            if (predicate(frame)) return frames;
        }

        throw new Xunit.Sdk.XunitException("expected barrier frame was not received");
    }

    private static async Task<Dictionary<string, object?>> ReceiveFrameAsync(ClientWebSocket socket)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var message = await WebSocketMessageReader.ReadAsync(socket, 1_048_576, timeout.Token);
        var decoder = new ControlFrameDecoder();
        var text = Encoding.UTF8.GetString(message.Payload);
        return decoder.Feed(text).OfType<ControlChunk>().First().Control;
    }

    private static string? Type(IReadOnlyDictionary<string, object?> frame) =>
        frame.TryGetValue("type", out var value) ? value?.ToString() : null;

    private static IReadOnlyList<Dictionary<string, object?>> DecodeBrowserFrames(RecordingBrowser browser) =>
        DecodeBrowserFrames(browser.Payloads);

    private static IReadOnlyList<Dictionary<string, object?>> DecodeBrowserFrames(
        DelayedDisconnectBrowser browser) => DecodeBrowserFrames(browser.Payloads);

    private static IReadOnlyList<Dictionary<string, object?>> DecodeBrowserFrames(
        IReadOnlyList<string> payloads) =>
        payloads
            .SelectMany(payload => new ControlFrameDecoder().Feed(payload))
            .OfType<ControlChunk>()
            .Select(chunk => chunk.Control)
            .ToArray();

    private static bool Bool(IReadOnlyDictionary<string, object?> frame, string key) =>
        frame.TryGetValue(key, out var value) && value is true;

    private static int QueuedLifecycleTransitionCount(WorkerTermState state)
    {
        var field = typeof(WorkerTermState).GetField(
            "LifecycleTransitionQueue",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
        Assert.NotNull(field);
        var queue = Assert.IsAssignableFrom<System.Collections.ICollection>(field.GetValue(state));
        return queue.Count;
    }

    private static async Task WaitUntilAsync(Func<bool> predicate)
    {
        for (var i = 0; i < 200 && !predicate(); i++) await Task.Delay(10);
        Assert.True(predicate());
    }

    private static int ResumeTokenCount(UtermServer server)
    {
        var field = typeof(UtermServer).GetField("_resumeTokens", BindingFlags.Instance | BindingFlags.NonPublic)
            ?? throw new Xunit.Sdk.XunitException("resume-token store field was not found");
        return ((ResumeTokenStore)field.GetValue(server)!).Count;
    }

    private static async Task<ClientWebSocket> ConnectAsync(Fixture fixture)
    {
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer " + fixture.Token);
        await socket.ConnectAsync(fixture.Uri, CancellationToken.None);
        return socket;
    }

    private static async Task<Fixture> BootAsync(
        Action<bool, string?>? onHijackChanged = null)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "resume-worker",
            DisplayName = "Resume Worker",
            Visibility = "public",
            Owner = "admin",
            InputMode = InputModes.Hijack,
            AutoStart = false,
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "resume-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            OnHijackChanged = onHijackChanged is null
                ? null
                : (_, enabled, owner) => onHijackChanged(enabled, owner),
        });
        var worker = new RecordingWorker();
        hub.Conn.RegisterWorker("resume-worker", worker);
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
        return new Fixture(
            server,
            hub,
            worker,
            token,
            new Uri($"ws://127.0.0.1:{port}/ws/browser/resume-worker/term"));
    }

    private static UtermServer NewUnstartedServer(TermHub hub)
    {
        var cfg = UtermServerConfig.Default();
        return new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = new RealClock(),
        });
    }

    private static UtermServer NewUnstartedServer(
        TermHub hub,
        string sessionId,
        out InMemorySessionRegistry registry)
    {
        var cfg = UtermServerConfig.Default();
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = sessionId,
            DisplayName = sessionId,
            Visibility = "public",
            InputMode = InputModes.Hijack,
            AutoStart = false,
        });
        registry = new InMemorySessionRegistry(cfg.Sessions);
        return new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = registry,
            Clock = new RealClock(),
        });
    }
}
