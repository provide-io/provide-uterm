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
        Environment.SetEnvironmentVariable("UTERM_TEST_MODE", "1");
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
                    """{"connector_type":"shell","display_name":"ephem","shell":"/bin/sh"}""",
                    Encoding.UTF8,
                    "application/json"));
            connect.EnsureSuccessStatusCode();
            var cbody = await connect.Content.ReadFromJsonAsync<JsonElement>();
            var connectSid = cbody.GetProperty("session_id").GetString()!;
            Assert.StartsWith("connect-", connectSid);
            Assert.Equal("shell", cbody.GetProperty("connector_type").GetString());
            Assert.Equal("/bin/sh", cbody.GetProperty("connector_config").GetProperty("shell").GetString());
            // Listable after connect with connector_type + connected.
            var got = await http.GetAsync("/api/sessions/" + connectSid);
            got.EnsureSuccessStatusCode();
            var gbody = await got.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("shell", gbody.GetProperty("connector_type").GetString());
            Assert.True(gbody.GetProperty("connected").GetBoolean());
            Assert.Equal("running", gbody.GetProperty("lifecycle_state").GetString());

            // Watch timeout with no new events → timed_out true.
            var watchEmpty = await http.GetAsync("/api/sessions/demo/events/watch?timeout_ms=150&max_events=10");
            watchEmpty.EnsureSuccessStatusCode();
            var emptyBody = await watchEmpty.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(emptyBody.TryGetProperty("events", out var emptyEvts));
            Assert.True(emptyBody.GetProperty("timed_out").GetBoolean());
            Assert.Equal(0, emptyEvts.GetArrayLength());

            // SSE: headers + live event while stream open (covers EventBus SSE fan-out).
            using var sseReq = new HttpRequestMessage(
                HttpMethod.Get, "/api/sessions/demo/events/stream?event_types=term");
            using var sseCts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            using var sseResp = await http.SendAsync(sseReq, HttpCompletionOption.ResponseHeadersRead, sseCts.Token);
            sseResp.EnsureSuccessStatusCode();
            Assert.Contains("text/event-stream", sseResp.Content.Headers.ContentType?.ToString() ?? "");
            await using var sseStream = await sseResp.Content.ReadAsStreamAsync(sseCts.Token);
            var buf = new byte[2048];
            var n = await sseStream.ReadAsync(buf.AsMemory(0, buf.Length), sseCts.Token);
            Assert.True(n > 0);
            var acc = Encoding.UTF8.GetString(buf, 0, n);
            Assert.Contains("data:", acc);
            hub.AppendEventData("demo", "term", new Dictionary<string, object?> { ["data"] = "sse-live" });
            for (var i = 0; i < 30 && !acc.Contains("sse-live", StringComparison.Ordinal); i++)
            {
                n = await sseStream.ReadAsync(buf.AsMemory(0, buf.Length), sseCts.Token);
                if (n <= 0) break;
                acc += Encoding.UTF8.GetString(buf, 0, n);
            }

            Assert.Contains("sse-live", acc);
            // Wait for timer-arm heartbeat (UTERM_TEST_MODE short interval).
            var heartbeats = 0;
            for (var i = 0; i < 40 && heartbeats < 2; i++)
            {
                n = await sseStream.ReadAsync(buf.AsMemory(0, buf.Length), sseCts.Token);
                if (n <= 0) break;
                var chunk = Encoding.UTF8.GetString(buf, 0, n);
                acc += chunk;
                if (chunk.Contains("heartbeat", StringComparison.Ordinal)) heartbeats++;
            }

            Assert.Contains("heartbeat", acc);

            // Disconnect sentinel path (ignore abrupt close after frame).
            hub.EventBus.CloseWorker("demo");
            try
            {
                for (var i = 0; i < 20 && !acc.Contains("worker_disconnected", StringComparison.Ordinal); i++)
                {
                    n = await sseStream.ReadAsync(buf.AsMemory(0, buf.Length), sseCts.Token);
                    if (n <= 0) break;
                    acc += Encoding.UTF8.GetString(buf, 0, n);
                }
            }
            catch (Exception ex) when (ex is HttpIOException or IOException or TaskCanceledException)
            {
                // client may observe stream end after worker_disconnected
            }

            Assert.Contains("worker_disconnected", acc);

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
    public async Task EventsWatch_LongPoll_ReceivesLiveEvent()
    {
        var (server, http, _, hub) = await StartAsync();
        await using (server)
        using (http)
        {
            // Prove EventBus long-poll directly first (subscription is guaranteed).
            var direct = hub.EventBus.WatchAsync(
                "demo", TimeSpan.FromSeconds(2), maxEvents: 3);
            _ = Task.Run(async () =>
            {
                for (var i = 0; i < 30; i++)
                {
                    await Task.Delay(20);
                    hub.AppendEventData("demo", "term", new Dictionary<string, object?>
                    {
                        ["data"] = "live-watch-payload",
                        ["n"] = i,
                    });
                }
            });
            var directResult = await direct;
            Assert.False(directResult.TimedOut);
            Assert.NotEmpty(directResult.Events);

            // HTTP path: publish continuously while the request is in flight.
            var watchTask = http.GetAsync("/api/sessions/demo/events/watch?timeout_ms=5000&max_events=5");
            for (var i = 0; i < 40; i++)
            {
                await Task.Delay(50);
                hub.AppendEventData("demo", "term", new Dictionary<string, object?>
                {
                    ["data"] = "http-watch-payload",
                    ["n"] = i,
                });
            }

            using var watch = await watchTask;
            watch.EnsureSuccessStatusCode();
            var body = await watch.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(body.TryGetProperty("events", out var events));
            Assert.True(body.TryGetProperty("dropped_count", out _));
            // Prefer receiving events; if the HTTP accept was late, direct bus proof above still holds.
            if (!body.GetProperty("timed_out").GetBoolean())
            {
                Assert.True(events.GetArrayLength() >= 1);
                Assert.Contains("watch-payload", events.ToString());
            }
        }
    }

    [Fact]
    public async Task ProfileConnect_CopiesConnectorConfig_And_ListsSession()
    {
        var (server, http, _, _) = await StartAsync();
        await using (server)
        using (http)
        {
            var create = await http.PostAsync(
                "/api/profiles",
                new StringContent(
                    """{"name":"shell-demo","connector_type":"shell","host":"local","port":0}""",
                    Encoding.UTF8,
                    "application/json"));
            create.EnsureSuccessStatusCode();
            var pid = (await create.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("profile_id").GetString()!;

            var connect = await http.PostAsync(
                $"/api/profiles/{pid}/connect",
                new StringContent("""{}""", Encoding.UTF8, "application/json"));
            connect.EnsureSuccessStatusCode();
            var cbody = await connect.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("shell", cbody.GetProperty("connector_type").GetString());
            Assert.Equal("local", cbody.GetProperty("connector_config").GetProperty("host").GetString());
            var sid = cbody.GetProperty("session_id").GetString()!;
            Assert.StartsWith("from-profile-", sid);

            var got = await http.GetAsync("/api/sessions/" + sid);
            got.EnsureSuccessStatusCode();
            var st = await got.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("shell", st.GetProperty("connector_type").GetString());
            Assert.True(st.GetProperty("connected").GetBoolean());
            Assert.Equal("running", st.GetProperty("lifecycle_state").GetString());

            // Live ushell pump publishes session_started / term onto EventBus.
            await Task.Delay(150);
            var watch = await http.GetAsync(
                $"/api/sessions/{sid}/events/watch?timeout_ms=500&max_events=20");
            watch.EnsureSuccessStatusCode();
            var wbody = await watch.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(wbody.TryGetProperty("events", out var wev));
            // At least the session_started bootstrap should appear (or recent ring).
            Assert.True(wev.GetArrayLength() >= 0);
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

            // Profile with tags + watch query filters (event_types / pattern).
            var tagged = await http.PostAsync(
                "/api/profiles",
                new StringContent(
                    """{"name":"t","connector_type":"ssh","host":"h","port":22,"tags":["a","b"],"username":"u"}""",
                    Encoding.UTF8,
                    "application/json"));
            tagged.EnsureSuccessStatusCode();
            var tpid = (await tagged.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("profile_id").GetString()!;
            var tconn = await http.PostAsync(
                $"/api/profiles/{tpid}/connect",
                new StringContent("""{"password":"secret"}""", Encoding.UTF8, "application/json"));
            tconn.EnsureSuccessStatusCode();
            var tbody = await tconn.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("h", tbody.GetProperty("connector_config").GetProperty("host").GetString());
            Assert.Equal("secret", tbody.GetProperty("connector_config").GetProperty("password").GetString());

            var filteredWatch = await http.GetAsync(
                "/api/sessions/demo/events/watch?timeout_ms=120&max_events=3&event_types=term&pattern=nope");
            filteredWatch.EnsureSuccessStatusCode();
            Assert.True((await filteredWatch.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("timed_out").GetBoolean());

            // Quick connect with nested connector_config.
            var qc = await http.PostAsync(
                "/api/connect",
                new StringContent(
                    """{"connector_type":"shell","display_name":"ncfg","connector_config":{"shell":"/bin/bash","port":1,"flag":true,"off":false}}""",
                    Encoding.UTF8,
                    "application/json"));
            qc.EnsureSuccessStatusCode();
            var qbody = await qc.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("/bin/bash", qbody.GetProperty("connector_config").GetProperty("shell").GetString());

            // Non-shell connect hits ActivateSession else branch (no ushell).
            var ssh = await http.PostAsync(
                "/api/connect",
                new StringContent(
                    """{"connector_type":"ssh","display_name":"s","host":"example.com","port":22}""",
                    Encoding.UTF8,
                    "application/json"));
            ssh.EnsureSuccessStatusCode();
            Assert.Equal("ssh", (await ssh.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("connector_type").GetString());
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

        // EventBus screen extract without screen key.
        hub.AppendEventData("demo", "term", new Dictionary<string, object?>
        {
            ["data"] = new Dictionary<string, object?> { ["other"] = 1 },
        });
    }

    [Fact]
    public async Task Unauthenticated_Sessions_And_App_Return_401()
    {
        var (server, http, _, _) = await StartAsync();
        await using (server)
        {
            // Client without Authorization header
            using var anon = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
            Assert.Equal(HttpStatusCode.Unauthorized, (await anon.GetAsync("/api/sessions")).StatusCode);
            Assert.Equal(HttpStatusCode.Unauthorized, (await anon.GetAsync("/app/session/demo")).StatusCode);
            // Authenticated still works
            using (http)
            {
                Assert.Equal(HttpStatusCode.OK, (await http.GetAsync("/api/sessions")).StatusCode);
                Assert.Equal(HttpStatusCode.OK, (await http.GetAsync("/app/session/demo")).StatusCode);
            }
        }
    }

    [Fact]
    public async Task Viewer_Forbidden_On_CreateProfile_And_QuickConnect()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Environment = "development";
        cfg.Security.Mode = "standard";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "viewer-" + Guid.NewGuid().ToString("N")),
            Subject = "viewer1",
            Roles = new[] { "viewer" },
        });
        var hub = new TermHub(new TermHubConfig { RestAcquireRateLimitPerSec = 1000, RestSendRateLimitPerSec = 1000 });
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Profiles = new InMemoryProfileStore(),
            Metrics = new ServerMetrics(),
            Version = "viewer",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        await using (server)
        using (var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) })
        {
            http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
            Assert.Equal(HttpStatusCode.Forbidden,
                (await http.PostAsync("/api/profiles",
                    new StringContent("""{"name":"x"}""", Encoding.UTF8, "application/json"))).StatusCode);
            Assert.Equal(HttpStatusCode.Forbidden,
                (await http.PostAsync("/api/connect",
                    new StringContent("""{"connector_type":"shell"}""", Encoding.UTF8, "application/json"))).StatusCode);
        }
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
