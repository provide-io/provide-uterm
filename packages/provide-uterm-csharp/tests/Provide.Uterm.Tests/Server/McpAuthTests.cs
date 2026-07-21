//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests.Server;

/// <summary>M7: MCP endpoint rejects query tokens and requires authentication.</summary>
public class McpAuthTests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, string BaseUrl)> StartAsync()
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
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "dev-user",
        });
        _ = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-mcp-token-" + Guid.NewGuid().ToString("N")),
            Subject = "dev-user",
            Roles = new[] { "admin" },
        });
        var apiKeys = new ApiKeyStore();
        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock, WorkerToken = cfg.Auth.WorkerBearerToken });
        hub.Conn.RegisterWorker("demo", new EchoWorker());
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = authz,
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "test",
            Clock = clock,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}");
    }

    private sealed class EchoWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }

    [Fact]
    public async Task Mcp_Rejects_Token_Query_Parameter()
    {
        var (server, baseUrl) = await StartAsync();
        await using (server)
        {
            using var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
            var resp = await http.GetAsync("/mcp?token=leak");
            Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
            var body = await resp.Content.ReadAsStringAsync();
            Assert.Contains("token query parameter is not allowed", body, StringComparison.Ordinal);
        }
    }

    [Fact]
    public async Task Mcp_Unauthenticated_Is_Rejected()
    {
        var (server, baseUrl) = await StartAsync();
        await using (server)
        {
            using var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
            var req = new HttpRequestMessage(HttpMethod.Get, "/mcp");
            req.Headers.TryAddWithoutValidation("Connection", "Upgrade");
            req.Headers.TryAddWithoutValidation("Upgrade", "websocket");
            req.Headers.TryAddWithoutValidation("Sec-WebSocket-Version", "13");
            req.Headers.TryAddWithoutValidation("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==");
            var resp = await http.SendAsync(req);
            Assert.True(
                resp.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden or HttpStatusCode.BadRequest,
                $"unexpected status {resp.StatusCode}");
        }
    }
}
