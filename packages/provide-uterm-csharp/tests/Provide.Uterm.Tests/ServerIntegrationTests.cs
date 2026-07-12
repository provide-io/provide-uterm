//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Client;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

public class ServerIntegrationTests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, string BaseUrl, string Token)> StartServerAsync()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo Session",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "dev-user",
        });

        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-test-dev-token-" + Guid.NewGuid().ToString("N")),
            Subject = "dev-user",
            Roles = new[] { "admin" },
        });

        var apiKeys = new ApiKeyStore();
        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock, WorkerToken = cfg.Auth.WorkerBearerToken });
        // Register a fake worker so hijack acquire can succeed.
        hub.Conn.RegisterWorker("demo", new EchoWorker());
        hub.Conn.RegisterWorker("adhoc-w", new EchoWorker());

        var registry = new InMemorySessionRegistry(cfg.Sessions);
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = authz,
            Config = cfg,
            Registry = registry,
            Version = "test",
            Clock = clock,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}", token);
    }

    [Fact]
    public async Task Health_And_Sessions_Work()
    {
        var (server, baseUrl, token) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);

            var health = await client.HealthAsync();
            Assert.True(health.TryGetValue("ok", out var ok) && ok is true or true);
            Assert.Equal("ok", health["status"]?.ToString());
            Assert.Equal("uterm-server", health["service"]?.ToString());

            var sessions = await client.ListSessionsAsync();
            Assert.NotNull(sessions);

            // Sessions should include demo
            var json = JsonSerializer.Serialize(sessions);
            Assert.Contains("demo", json, StringComparison.Ordinal);

            var one = await client.GetSessionAsync("demo");
            Assert.Equal("demo", one["session_id"]?.ToString());
        }
    }

    [Fact]
    public async Task Hijack_Lifecycle_Via_Client()
    {
        var (server, baseUrl, token) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);

            var acq = await client.AcquireAsync("demo", owner: "operator", leaseS: 60);
            Assert.True(acq.TryGetValue("ok", out var ok) && ok is true);
            var hijackId = acq["hijack_id"]?.ToString();
            Assert.False(string.IsNullOrEmpty(hijackId));

            var hb = await client.HeartbeatAsync("demo", hijackId!, 60);
            Assert.True(hb.TryGetValue("ok", out var ok2) && ok2 is true);

            var snap = await client.SnapshotAsync("demo", hijackId!);
            Assert.True(snap.TryGetValue("ok", out var ok3) && ok3 is true);

            var send = await client.SendAsync("demo", hijackId!, "hello");
            Assert.True(send.TryGetValue("ok", out var ok4) && ok4 is true);

            var rel = await client.ReleaseAsync("demo", hijackId!);
            Assert.True(rel.TryGetValue("ok", out var ok5) && ok5 is true);
        }
    }

    [Fact]
    public async Task Healthz_Is_Anonymous()
    {
        var (server, baseUrl, _) = await StartServerAsync();
        await using (server)
        {
            using var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
            var resp = await http.GetAsync("/healthz");
            resp.EnsureSuccessStatusCode();
            var body = await resp.Content.ReadAsStringAsync();
            Assert.Contains("ok", body, StringComparison.Ordinal);
        }
    }

    private sealed class EchoWorker : IWorkerWs
    {
        public List<string> Sent { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Sent.Add(payload);
            return Task.CompletedTask;
        }
    }
}
