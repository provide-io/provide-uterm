//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;

namespace Provide.Uterm.Tests;

/// <summary>
/// Live HTTP proof for C# control-plane REST + tunnel host lifecycle
/// (session control, webhooks, fan-out, /api/tunnels).
/// </summary>
public sealed partial class ServerIntegrationControlPlaneRestTests
{
    private static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var p = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    private static async Task<(UtermServer Server, HttpClient Http, string Token)> StartServerAsync(
        Action<UtermServerConfig>? configure = null)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Tunnel.CookieSecure = false;
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });
        configure?.Invoke(cfg);

        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "cp-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var hub = new TermHub(new TermHubConfig { RestAcquireRateLimitPerSec = 1000, RestSendRateLimitPerSec = 1000 });
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Webhooks = new WebhookManager(allowLoopbackDestinations: true),
            TunnelStore = new MemoryTunnelStore(),
            Version = "cp-test",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return (server, http, token);
    }

    [Fact]
    public async Task SessionControl_ConnectModeClearAnalyzeSnapshotEvents()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            var connect = await http.PostAsync("/api/sessions/demo/connect", null);
            connect.EnsureSuccessStatusCode();
            var body = await connect.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("running", body.GetProperty("lifecycle_state").GetString());

            var mode = await http.PostAsync(
                "/api/sessions/demo/mode",
                new StringContent("""{"input_mode":"open"}""", Encoding.UTF8, "application/json"));
            mode.EnsureSuccessStatusCode();
            var modeBody = await mode.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("open", modeBody.GetProperty("input_mode").GetString());

            var clear = await http.PostAsync("/api/sessions/demo/clear", null);
            clear.EnsureSuccessStatusCode();

            var analyze = await http.PostAsync("/api/sessions/demo/analyze", null);
            analyze.EnsureSuccessStatusCode();
            var analysis = await analyze.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("demo", analysis.GetProperty("session_id").GetString());
            Assert.Equal(JsonValueKind.String, analysis.GetProperty("analysis").ValueKind); // prose, not status

            var snap = await http.GetAsync("/api/sessions/demo/snapshot");
            snap.EnsureSuccessStatusCode();

            var events = await http.GetAsync("/api/sessions/demo/events?limit=10");
            events.EnsureSuccessStatusCode();

            var restart = await http.PostAsync("/api/sessions/demo/restart", null);
            restart.EnsureSuccessStatusCode();
            var disc = await http.PostAsync("/api/sessions/demo/disconnect", null);
            disc.EnsureSuccessStatusCode();
            var discBody = await disc.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("stopped", discBody.GetProperty("lifecycle_state").GetString()); // reference vocabulary

            var badMode = await http.PostAsync(
                "/api/sessions/demo/mode",
                new StringContent("""{"input_mode":"nope"}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.UnprocessableEntity, badMode.StatusCode);

            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync("/api/sessions/missing/connect", null)).StatusCode);
        }
    }

    [Fact]
    public async Task Webhooks_RegisterListDelete()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            var reg = await http.PostAsync(
                "/api/sessions/demo/webhooks",
                new StringContent(
                    """{"url":"http://127.0.0.1:9/hook","event_types":["snapshot"],"pattern":"WELCOME","secret":"s"}""",
                    Encoding.UTF8,
                    "application/json"));
            reg.EnsureSuccessStatusCode();
            var regBody = await reg.Content.ReadFromJsonAsync<JsonElement>();
            var wid = regBody.GetProperty("webhook_id").GetString();
            Assert.False(string.IsNullOrEmpty(wid));

            var list = await http.GetAsync("/api/sessions/demo/webhooks");
            list.EnsureSuccessStatusCode();
            var listBody = await list.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal(1, listBody.GetProperty("webhooks").GetArrayLength());

            var del = await http.DeleteAsync($"/api/sessions/demo/webhooks/{wid}");
            del.EnsureSuccessStatusCode();

            var list2 = await http.GetAsync("/api/sessions/demo/webhooks");
            var list2Body = await list2.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal(0, list2Body.GetProperty("webhooks").GetArrayLength());

            var noUrl = await http.PostAsync(
                "/api/sessions/demo/webhooks",
                new StringContent("""{}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.UnprocessableEntity, noUrl.StatusCode);
        }
    }

    [Fact]
    public async Task Fanout_CreateListSendGrantDelete()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            var create = await http.PostAsync(
                "/api/fanout/groups",
                new StringContent(
                    """{"name":"g1","worker_ids":["demo"],"mode":"parallel"}""",
                    Encoding.UTF8,
                    "application/json"));
            create.EnsureSuccessStatusCode();
            var cbody = await create.Content.ReadFromJsonAsync<JsonElement>();
            var gid = cbody.GetProperty("group_id").GetString();
            Assert.False(string.IsNullOrEmpty(gid));
            Assert.Equal(1, cbody.GetProperty("session_count").GetInt32());

            var list = await http.GetAsync("/api/fanout/groups");
            list.EnsureSuccessStatusCode();
            var lbody = await list.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal(1, lbody.GetArrayLength());

            var send = await http.PostAsync(
                $"/api/fanout/groups/{gid}/send",
                new StringContent("""{"data":"look\r"}""", Encoding.UTF8, "application/json"));
            send.EnsureSuccessStatusCode();
            var sbody = await send.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal(gid, sbody.GetProperty("group_id").GetString());
            Assert.True(sbody.TryGetProperty("send_id", out _));

            var grant = await http.PostAsync(
                $"/api/fanout/groups/{gid}/grants",
                new StringContent("""{"grantee":"other"}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.NoContent, grant.StatusCode);

            var del = await http.DeleteAsync($"/api/fanout/groups/{gid}");
            Assert.Equal(HttpStatusCode.NoContent, del.StatusCode);

            Assert.Equal(HttpStatusCode.NotFound,
                (await http.DeleteAsync("/api/fanout/groups/nope")).StatusCode);
        }
    }

    [Fact]
    public async Task Fanout_ConcurrentFirstUseSharesOneControllerAndStore()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            const int requests = 64;
            var creates = Enumerable.Range(0, requests).Select(index => http.PostAsync(
                "/api/fanout/groups",
                new StringContent(
                    $$"""{"name":"g{{index}}","worker_ids":["demo"]}""",
                    Encoding.UTF8,
                    "application/json")));
            var responses = await Task.WhenAll(creates);
            Assert.All(responses, response => response.EnsureSuccessStatusCode());

            var list = await http.GetAsync("/api/fanout/groups");
            list.EnsureSuccessStatusCode();
            var body = await list.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal(requests, body.GetArrayLength());
        }
    }

    [Fact]
    public async Task Fanout_Unknown_Members_Are_Strict_By_Default_And_Configurable()
    {
        var (strictServer, strictHttp, _) = await StartServerAsync();
        await using (strictServer)
        using (strictHttp)
        {
            var strict = await strictHttp.PostAsync(
                "/api/fanout/groups",
                new StringContent("""{"name":"g","worker_ids":["future-worker"]}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.BadRequest, strict.StatusCode);
        }

        var (permissiveServer, permissiveHttp, _) = await StartServerAsync(cfg => cfg.FanoutAllowUnknownMembers = true);
        await using (permissiveServer)
        using (permissiveHttp)
        {
            var permissive = await permissiveHttp.PostAsync(
                "/api/fanout/groups",
                new StringContent("""{"name":"g","worker_ids":["future-worker"]}""", Encoding.UTF8, "application/json"));
            permissive.EnsureSuccessStatusCode();
        }
    }

    [Fact]
    public async Task Fanout_Configured_Unsupported_Governance_Fails_Closed()
    {
        var (server, http, _) = await StartServerAsync(cfg => cfg.Governance.PolicyWebhookUrl = "https://policy.example.test");
        await using (server)
        using (http)
        {
            var create = await http.PostAsync(
                "/api/fanout/groups",
                new StringContent("""{"name":"g","worker_ids":["demo"]}""", Encoding.UTF8, "application/json"));
            create.EnsureSuccessStatusCode();
            var body = await create.Content.ReadFromJsonAsync<JsonElement>();
            var groupId = body.GetProperty("group_id").GetString();
            var send = await http.PostAsync(
                $"/api/fanout/groups/{groupId}/send",
                new StringContent("""{"data":"id"}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.NotImplemented, send.StatusCode);
        }
    }
}
