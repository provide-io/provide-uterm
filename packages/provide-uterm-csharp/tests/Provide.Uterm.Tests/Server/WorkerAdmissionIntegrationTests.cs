//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Server;

public sealed class WorkerAdmissionIntegrationTests
{
    [Theory]
    [InlineData("/ws/worker/rejected/term")]
    [InlineData("/tunnel/rejected")]
    public async Task InvalidBearerRemainsHttp401BeforeUpgrade(string path)
    {
        var fixture = await BootAtCapacityAsync();
        await using var server = fixture.Server;
        using var http = new HttpClient();
        using var request = new HttpRequestMessage(
            HttpMethod.Get,
            new Uri(fixture.WsBase.Replace("ws://", "http://", StringComparison.Ordinal) + path));
        request.Headers.TryAddWithoutValidation("Connection", "Upgrade");
        request.Headers.TryAddWithoutValidation("Upgrade", "websocket");
        request.Headers.TryAddWithoutValidation("Sec-WebSocket-Version", "13");
        request.Headers.TryAddWithoutValidation("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==");
        request.Headers.TryAddWithoutValidation("Authorization", "Bearer wrong");

        using var response = await http.SendAsync(request);

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Theory]
    [InlineData("/ws/worker/rejected/term")]
    [InlineData("/tunnel/rejected")]
    public async Task Endpoint_ClosesWhenWorkerRegistrationIsRefused(string path)
    {
        var fixture = await BootAtCapacityAsync();
        await using var server = fixture.Server;
        using var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer worker-secret");
        await socket.ConnectAsync(new Uri(fixture.WsBase + path), CancellationToken.None);

        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
        var result = await socket.ReceiveAsync(new byte[128], timeout.Token);

        Assert.Equal(WebSocketMessageType.Close, result.MessageType);
        Assert.Equal(WebSocketCloseStatus.PolicyViolation, result.CloseStatus);
        Assert.Null(fixture.Hub.Registry.Get("rejected"));
        Assert.True(fixture.Registry.TryGetStatus("rejected", out var status));
        Assert.False(status.Connected);
    }

    private static async Task<Fixture> BootAtCapacityAsync()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Auth.Mode = "dev_token";
        cfg.Auth.WorkerBearerToken = "worker-secret";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "rejected",
            DisplayName = "Rejected",
            Visibility = "public",
            AutoStart = false,
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            WorkerToken = cfg.Auth.WorkerBearerToken,
            MaxWorkers = 1,
        });
        Assert.True(hub.Conn.RegisterWorker("capacity-holder", new NoopSocket()));
        var registry = new InMemorySessionRegistry(cfg.Sessions);
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = registry,
            Clock = clock,
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        return new Fixture(server, hub, registry, $"ws://127.0.0.1:{port}");
    }

    private sealed record Fixture(
        UtermServer Server,
        TermHub Hub,
        InMemorySessionRegistry Registry,
        string WsBase);

    private sealed class NoopSocket : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }
}
