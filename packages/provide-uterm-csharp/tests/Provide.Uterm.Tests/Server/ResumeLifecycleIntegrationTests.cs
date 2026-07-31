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

namespace Provide.Uterm.Tests.Server;

public sealed class ResumeLifecycleIntegrationTests
{
    [Fact]
    public async Task CurrentOwnerTokenRestoresHijackAndReportsResumedTrue()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.HijackOwner is not null);

        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        await WaitUntilAsync(() => fixture.Worker.Actions.SequenceEqual(["pause", "resume"]));

        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        await SendControlAsync(resumedSocket, "resume", oldToken);
        var resumedHello = await ReceiveUntilAsync(
            resumedSocket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.True(Bool(resumedHello, "resumed"));
        Assert.True(Bool(resumedHello, "hijacked_by_me"));
        Assert.NotEqual(oldToken, resumedHello["resume_token"]?.ToString());
        Assert.NotNull(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        await WaitUntilAsync(() => fixture.Worker.Actions.Count == 3);
        Assert.Equal(["pause", "resume", "pause"], fixture.Worker.Actions);
    }

    [Fact]
    public async Task LaterOwnerMakesOldTokenTruthfullyReportResumedFalse()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        await Task.Delay(50);

        using var later = await ConnectAsync(fixture);
        await DrainHandshakeAsync(later);
        await SendControlAsync(later, "hijack_request");
        await ReceiveUntilAsync(later, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        await SendControlAsync(later, "hijack_release");
        await ReceiveUntilAsync(later, frame => Type(frame) == "hijack_state" && !Bool(frame, "hijacked"));

        using var attempted = await ConnectAsync(fixture);
        await DrainHandshakeAsync(attempted);
        await SendControlAsync(attempted, "resume", oldToken);
        var hello = await ReceiveUntilAsync(
            attempted, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.False(Bool(hello, "resumed"));
        Assert.False(Bool(hello, "hijacked_by_me"));
        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
    }

    [Fact]
    public async Task ConsumedNonOwnerTokenReportsFalseWithFreshUsableToken()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var socket = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(socket))["resume_token"]!.ToString()!;

        await SendControlAsync(socket, "resume", oldToken);
        var hello = await ReceiveUntilAsync(
            socket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.False(Bool(hello, "resumed"));
        Assert.NotEqual(oldToken, hello["resume_token"]?.ToString());
    }

    [Fact]
    public async Task DisconnectedNonOwnerTokenResumesOnNewSocketWithoutPausingWorker()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        var oldToken = (await DrainHandshakeAsync(original))["resume_token"]!.ToString()!;

        original.Abort();
        await WaitUntilAsync(() => fixture.Hub.Registry.Get("resume-worker")!.Browsers.Count == 0);
        Assert.Empty(fixture.Worker.Actions);

        using var resumedSocket = await ConnectAsync(fixture);
        await DrainHandshakeAsync(resumedSocket);
        await SendControlAsync(resumedSocket, "resume", oldToken);
        var hello = await ReceiveUntilAsync(
            resumedSocket, frame => Type(frame) == "hello" && frame.ContainsKey("resumed"));

        Assert.True(Bool(hello, "resumed"));
        Assert.False(Bool(hello, "hijacked_by_me"));
        Assert.Empty(fixture.Worker.Actions);
    }

    [Fact]
    public async Task RejectedDashboardRequestDuringDisconnectResumeDoesNotPauseWorker()
    {
        var fixture = await BootAsync();
        await using var server = fixture.Server;
        using var original = await ConnectAsync(fixture);
        await DrainHandshakeAsync(original);
        await SendControlAsync(original, "hijack_request");
        await ReceiveUntilAsync(original, frame => Type(frame) == "hijack_state" && Bool(frame, "hijacked"));
        using var contender = await ConnectAsync(fixture);
        await DrainHandshakeAsync(contender);
        var requestRejected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        fixture.Worker.AfterResume = async () =>
        {
            await SendControlAsync(contender, "hijack_request");
            await ReceiveUntilAsync(contender, frame => Type(frame) == "error");
            requestRejected.TrySetResult();
        };

        original.Abort();
        await requestRejected.Task.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Null(fixture.Hub.Registry.Get("resume-worker")!.HijackOwner);
        Assert.Equal(["pause", "resume"], fixture.Worker.Actions);
    }

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

    private static bool Bool(IReadOnlyDictionary<string, object?> frame, string key) =>
        frame.TryGetValue(key, out var value) && value is true;

    private static async Task WaitUntilAsync(Func<bool> predicate)
    {
        for (var i = 0; i < 200 && !predicate(); i++) await Task.Delay(10);
        Assert.True(predicate());
    }

    private static async Task<ClientWebSocket> ConnectAsync(Fixture fixture)
    {
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer " + fixture.Token);
        await socket.ConnectAsync(fixture.Uri, CancellationToken.None);
        return socket;
    }

    private static async Task<Fixture> BootAsync()
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
        var hub = new TermHub(new TermHubConfig { Clock = clock });
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

    private sealed record Fixture(
        UtermServer Server,
        TermHub Hub,
        RecordingWorker Worker,
        string Token,
        Uri Uri);

    private sealed class RecordingWorker : IWorkerWs
    {
        private readonly object _gate = new();
        private readonly List<string> _actions = [];

        public Func<Task>? AfterResume { get; set; }

        public IReadOnlyList<string> Actions
        {
            get { lock (_gate) return _actions.ToArray(); }
        }

        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            if (action is not null)
            {
                lock (_gate) _actions.Add(action);
                if (action == "resume" && AfterResume is not null) await AfterResume();
            }
        }
    }
}
