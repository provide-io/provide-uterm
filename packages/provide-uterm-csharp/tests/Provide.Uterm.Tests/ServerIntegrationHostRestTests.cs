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
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>Live HTTP proof for residual host REST (profiles, keys, approvals, metrics, posture, session extras, SPA shell).</summary>
public sealed class ServerIntegrationHostRestTests
{
    private static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var p = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    private static async Task<(UtermServer Server, HttpClient Http, string Token, TermHub Hub)> StartAsync()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Auth.ApiKeysEnabled = true;
        cfg.Environment = "development";
        cfg.Security.Mode = "standard";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "hostrest-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var hub = new TermHub(new TermHubConfig { RestAcquireRateLimitPerSec = 1000, RestSendRateLimitPerSec = 1000 });
        var apiKeys = new ApiKeyStore();
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, apiKeys),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            ApiKeys = apiKeys,
            Profiles = new InMemoryProfileStore(),
            Metrics = new ServerMetrics(),
            Version = "host-rest",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return (server, http, token, hub);
    }

    [Fact]
    public async Task Profiles_Crud_And_Connect()
    {
        var (server, http, _, _) = await StartAsync();
        await using (server)
        using (http)
        {
            var create = await http.PostAsync(
                "/api/profiles",
                new StringContent(
                    """{"name":"bbs","connector_type":"telnet","host":"bbs.example","port":23}""",
                    Encoding.UTF8,
                    "application/json"));
            create.EnsureSuccessStatusCode();
            var cbody = await create.Content.ReadFromJsonAsync<JsonElement>();
            var pid = cbody.GetProperty("profile_id").GetString()!;
            Assert.StartsWith("profile-", pid);

            Assert.True((await http.GetAsync("/api/profiles")).IsSuccessStatusCode);
            Assert.True((await http.GetAsync($"/api/profiles/{pid}")).IsSuccessStatusCode);

            var put = await http.PutAsync(
                $"/api/profiles/{pid}",
                new StringContent("""{"name":"bbs2"}""", Encoding.UTF8, "application/json"));
            put.EnsureSuccessStatusCode();
            Assert.Equal("bbs2", (await put.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("name").GetString());

            var conn = await http.PostAsync($"/api/profiles/{pid}/connect", null);
            conn.EnsureSuccessStatusCode();
            var connBody = await conn.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(connBody.GetProperty("ok").GetBoolean());
            Assert.StartsWith("from-profile-", connBody.GetProperty("session_id").GetString());

            Assert.True((await http.DeleteAsync($"/api/profiles/{pid}")).IsSuccessStatusCode);
            Assert.Equal(HttpStatusCode.NotFound, (await http.GetAsync($"/api/profiles/{pid}")).StatusCode);
        }
    }

    [Fact]
    public async Task ApiKeys_CreateListRevoke()
    {
        var (server, http, _, _) = await StartAsync();
        await using (server)
        using (http)
        {
            var create = await http.PostAsync(
                "/api/keys",
                new StringContent(
                    """{"name":"ci","scopes":["operator","viewer"]}""",
                    Encoding.UTF8,
                    "application/json"));
            create.EnsureSuccessStatusCode();
            var cbody = await create.Content.ReadFromJsonAsync<JsonElement>();
            Assert.False(string.IsNullOrEmpty(cbody.GetProperty("key").GetString()));
            var kid = cbody.GetProperty("key_id").GetString()!;

            var list = await http.GetAsync("/api/keys");
            list.EnsureSuccessStatusCode();
            Assert.True((await list.Content.ReadFromJsonAsync<JsonElement>()).GetArrayLength() >= 1);

            Assert.True((await http.DeleteAsync($"/api/keys/{kid}")).IsSuccessStatusCode);
            Assert.Equal(HttpStatusCode.NotFound, (await http.DeleteAsync($"/api/keys/{kid}")).StatusCode);
        }
    }

    [Fact]
    public async Task Approvals_ListApproveReject()
    {
        var (server, http, _, hub) = await StartAsync();
        await using (server)
        using (http)
        {
            hub.Approvals.Add(new ApprovalRequest
            {
                Id = "apr-1",
                WorkerId = "demo",
                SubmitterId = "other-user",
                Command = "rm -rf /",
                CreatedAt = 1,
                ExpiresAt = 9_999_999,
            });
            hub.Approvals.Add(new ApprovalRequest
            {
                Id = "apr-2",
                WorkerId = "demo",
                SubmitterId = "other-user",
                Command = "ls",
                CreatedAt = 1,
                ExpiresAt = 9_999_999,
            });

            var list = await http.GetAsync("/api/approvals");
            list.EnsureSuccessStatusCode();
            Assert.Equal(2, (await list.Content.ReadFromJsonAsync<JsonElement>()).GetArrayLength());

            var ap = await http.PostAsync("/api/approvals/apr-1/approve", null);
            ap.EnsureSuccessStatusCode();
            Assert.Equal("approved", (await ap.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("status").GetString());

            var rj = await http.PostAsync("/api/approvals/apr-2/reject", null);
            rj.EnsureSuccessStatusCode();
            Assert.Equal("rejected", (await rj.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("status").GetString());
        }
    }

    [Fact]
    public async Task Metrics_Posture_SessionPatch_Bulk_Connect_Events_Spa()
    {
        var (server, http, _, hub) = await StartAsync();
        await using (server)
        using (http)
        {
            hub.AppendEventData("demo", "term", new Dictionary<string, object?> { ["data"] = "hi" });

            var metrics = await http.GetAsync("/api/metrics");
            metrics.EnsureSuccessStatusCode();
            Assert.True((await metrics.Content.ReadFromJsonAsync<JsonElement>()).TryGetProperty("metrics", out _));

            var prom = await http.GetAsync("/api/metrics/prometheus");
            prom.EnsureSuccessStatusCode();

            var posture = await http.GetAsync("/api/security-posture");
            posture.EnsureSuccessStatusCode();
            var pbody = await posture.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(pbody.TryGetProperty("secure", out _));
            Assert.True(pbody.TryGetProperty("auth_mode", out _)); // admin sees full

            var patch = await http.PatchAsync(
                "/api/sessions/demo",
                new StringContent(
                    """{"display_name":"Demo2","tags":["a"]}""",
                    Encoding.UTF8,
                    "application/json"));
            patch.EnsureSuccessStatusCode();
            Assert.Equal("Demo2", (await patch.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("display_name").GetString());

            var connect = await http.PostAsync(
                "/api/connect",
                new StringContent(
                    """{"connector_type":"shell","display_name":"ephem"}""",
                    Encoding.UTF8,
                    "application/json"));
            connect.EnsureSuccessStatusCode();
            Assert.StartsWith("connect-", (await connect.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("session_id").GetString());

            var watch = await http.GetAsync("/api/sessions/demo/events/watch?timeout_ms=100&max_events=10");
            watch.EnsureSuccessStatusCode();
            Assert.True((await watch.Content.ReadFromJsonAsync<JsonElement>()).TryGetProperty("events", out _));

            // SSE: headers + first chunk only (stream is long-lived)
            using var sseReq = new HttpRequestMessage(HttpMethod.Get, "/api/sessions/demo/events/stream");
            using var sseCts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            using var sseResp = await http.SendAsync(sseReq, HttpCompletionOption.ResponseHeadersRead, sseCts.Token);
            sseResp.EnsureSuccessStatusCode();
            Assert.Contains("text/event-stream", sseResp.Content.Headers.ContentType?.ToString() ?? "");
            await using var sseStream = await sseResp.Content.ReadAsStreamAsync(sseCts.Token);
            var buf = new byte[512];
            var n = await sseStream.ReadAsync(buf.AsMemory(0, buf.Length), sseCts.Token);
            Assert.True(n > 0);
            Assert.Contains("data:", Encoding.UTF8.GetString(buf, 0, n));

            var spa = await http.GetAsync("/app/inspect/demo");
            // Inspect page is mapped by tunnel routes; app shell also available
            var appShell = await http.GetAsync("/app/session/demo");
            appShell.EnsureSuccessStatusCode();
            var html = await appShell.Content.ReadAsStringAsync();
            Assert.Contains("app-root", html);

            var bulk = await http.DeleteAsync("/api/sessions?lifecycle_state=running");
            bulk.EnsureSuccessStatusCode();
            Assert.True((await bulk.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("deleted").GetInt32() >= 0);
        }
    }

    [Fact]
    public async Task HostRest_ErrorBranches_And_PureStores()
    {
        var (server, http, _, hub) = await StartAsync();
        await using (server)
        using (http)
        {
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await http.PostAsync("/api/keys",
                    new StringContent("""{"name":"","scopes":["admin"]}""", Encoding.UTF8, "application/json"))).StatusCode);
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await http.PostAsync("/api/keys",
                    new StringContent("""{"name":"x","scopes":["nope"]}""", Encoding.UTF8, "application/json"))).StatusCode);
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await http.PostAsync("/api/keys",
                    new StringContent("""{"name":"x","scopes":["admin"],"tenant_id":"t"}""", Encoding.UTF8, "application/json"))).StatusCode);
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await http.PostAsync("/api/keys",
                    new StringContent("""{"name":"x","scopes":["admin"],"expires_in_s":10}""", Encoding.UTF8, "application/json"))).StatusCode);

            Assert.Equal(HttpStatusCode.NotFound, (await http.GetAsync("/api/profiles/missing")).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound, (await http.DeleteAsync("/api/profiles/missing")).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync("/api/profiles/missing/connect", null)).StatusCode);

            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync("/api/approvals/nope/approve", null)).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync("/api/approvals/nope/reject", null)).StatusCode);

            hub.Approvals.Add(new ApprovalRequest
            {
                Id = "self",
                WorkerId = "demo",
                SubmitterId = "admin",
                Command = "x",
                CreatedAt = 1,
                ExpiresAt = 9e9,
            });
            Assert.Equal(HttpStatusCode.Forbidden,
                (await http.PostAsync("/api/approvals/self/approve", null)).StatusCode);

            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PatchAsync("/api/sessions/missing",
                    new StringContent("""{"display_name":"n"}""", Encoding.UTF8, "application/json"))).StatusCode);
        }

        // Pure store/metrics coverage
        var store = new InMemoryProfileStore();
        var p = store.CreateProfile(new ConnectionProfile
        {
            ProfileId = "p1", Owner = "o", Name = "n", CreatedAt = 1, UpdatedAt = 1,
        });
        Assert.NotNull(store.GetProfile("p1"));
        Assert.Single(store.ListProfiles("o"));
        Assert.Empty(store.ListProfiles("other"));
        store.UpdateProfile("p1", x => x.Name = "n2");
        Assert.Equal("n2", store.GetProfile("p1")!.Name);
        Assert.True(store.DeleteProfile("p1"));
        Assert.False(store.DeleteProfile("p1"));

        var m = new ServerMetrics();
        m.Inc("a");
        m.Inc("a");
        m.Inc("b", 3);
        Assert.Equal(2, m.Snapshot()["a"]);
        Assert.Contains("uterm_a", m.Prometheus());
        Assert.Contains("uterm_b", m.Prometheus());

        var pending = hub.Approvals.PendingApprovals();
        Assert.NotNull(pending);
    }

    [Fact]
    public async Task StaticUi_PhysicalFrontendDir_And_MetricsAuth()
    {
        var fe = Path.Combine(Path.GetTempPath(), "uterm-fe-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(fe);
        await File.WriteAllTextAsync(Path.Combine(fe, "index.html"),
            "<!DOCTYPE html><html><body><div id=\"app-root\">SPA</div></body></html>");
        await File.WriteAllTextAsync(Path.Combine(fe, "terminal.html"),
            "<!DOCTYPE html><html><body>term</body></html>");

        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Auth.ApiKeysEnabled = true;
        cfg.Security.MetricsRequireAuth = true;
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo", DisplayName = "D", ConnectorType = "shell",
            Visibility = "public", Owner = "admin",
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "spa-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig()),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            FrontendDir = fe,
            Metrics = new ServerMetrics(),
            Profiles = new InMemoryProfileStore(),
            ApiKeys = new ApiKeyStore(),
            Version = "spa",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        using var anon = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        Assert.Equal(HttpStatusCode.Unauthorized, (await anon.GetAsync("/api/metrics")).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await anon.GetAsync("/api/metrics/prometheus")).StatusCode);

        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        Assert.True((await http.GetAsync("/api/metrics")).IsSuccessStatusCode);
        var spa = await http.GetAsync("/app/session/x");
        spa.EnsureSuccessStatusCode();
        Assert.Contains("app-root", await spa.Content.ReadAsStringAsync());

        try { Directory.Delete(fe, true); } catch { /* ignore */ }
    }
}
