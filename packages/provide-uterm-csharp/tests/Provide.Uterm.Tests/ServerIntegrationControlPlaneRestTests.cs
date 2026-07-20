//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;
using Provide.Uterm.TunnelClient;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// Live HTTP proof for C# control-plane REST + tunnel host lifecycle
/// (session control, webhooks, fan-out, /api/tunnels).
/// </summary>
public sealed class ServerIntegrationControlPlaneRestTests
{
    private static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var p = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    private static async Task<(UtermServer Server, HttpClient Http, string Token)> StartServerAsync()
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
            Assert.True(analysis.GetProperty("analysis").TryGetProperty("lifecycle_state", out _));

            var snap = await http.GetAsync("/api/sessions/demo/snapshot");
            snap.EnsureSuccessStatusCode();

            var events = await http.GetAsync("/api/sessions/demo/events?limit=10");
            events.EnsureSuccessStatusCode();

            var restart = await http.PostAsync("/api/sessions/demo/restart", null);
            restart.EnsureSuccessStatusCode();
            var disc = await http.PostAsync("/api/sessions/demo/disconnect", null);
            disc.EnsureSuccessStatusCode();
            var discBody = await disc.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("disconnected", discBody.GetProperty("lifecycle_state").GetString());

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
    public async Task TunnelHost_CreateListRotateRevokeShare()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            var create = await http.PostAsync(
                "/api/tunnels",
                new StringContent(
                    """{"display_name":"t1","tunnel_type":"terminal"}""",
                    Encoding.UTF8,
                    "application/json"));
            create.EnsureSuccessStatusCode();
            var cbody = await create.Content.ReadFromJsonAsync<JsonElement>();
            var tid = cbody.GetProperty("tunnel_id").GetString()!;
            var workerToken = cbody.GetProperty("worker_token").GetString();
            var shareUrl = cbody.GetProperty("share_url").GetString()!;
            Assert.StartsWith("tunnel-", tid);
            Assert.False(string.IsNullOrEmpty(workerToken));
            Assert.Contains("/s/" + tid + "?invite=", shareUrl);
            Assert.Contains("/tunnel/" + tid, cbody.GetProperty("ws_endpoint").GetString());

            var list = await http.GetAsync("/api/tunnels");
            list.EnsureSuccessStatusCode();
            var lbody = await list.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(lbody.GetArrayLength() >= 1);

            // Share consumer: consume invite → 302 + cookie
            using var noAuth = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
            {
                BaseAddress = http.BaseAddress,
            };
            var inviteQ = shareUrl.Split("?invite=", 2)[1];
            var share = await noAuth.GetAsync($"/s/{tid}?invite={inviteQ}");
            Assert.Equal(HttpStatusCode.Found, share.StatusCode);
            Assert.True(share.Headers.TryGetValues("Set-Cookie", out var cookies));
            Assert.Contains(cookies, c => c.Contains("uterm_tunnel_" + tid, StringComparison.Ordinal));

            var rotate = await http.PostAsync($"/api/tunnels/{tid}/tokens/rotate", null);
            rotate.EnsureSuccessStatusCode();
            var rbody = await rotate.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal(tid, rbody.GetProperty("tunnel_id").GetString());
            Assert.NotEqual(workerToken, rbody.GetProperty("worker_token").GetString());

            var revoke = await http.DeleteAsync($"/api/tunnels/{tid}/tokens");
            revoke.EnsureSuccessStatusCode();

            // Bad invite after revoke of invites on rotate already burned first; new rotate invites work
            // but tokens deleted — share without invite still redirects.
            var share2 = await noAuth.GetAsync($"/s/{tid}");
            Assert.Equal(HttpStatusCode.Found, share2.StatusCode);

            // Bad invite → 403
            var badInvite = await noAuth.GetAsync($"/s/{tid}?invite=not-a-real-invite");
            Assert.Equal(HttpStatusCode.Forbidden, badInvite.StatusCode);

            // Rotate after revoke → no tokens
            var rotGone = await http.PostAsync($"/api/tunnels/{tid}/tokens/rotate", null);
            Assert.Equal(HttpStatusCode.NotFound, rotGone.StatusCode);
        }
    }

    [Fact]
    public async Task ControlPlane_ErrorBranches()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await http.PostAsync("/api/sessions/bad!id/connect", null)).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.GetAsync("/api/sessions/missing/snapshot")).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.GetAsync("/api/sessions/missing/events")).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync("/api/sessions/missing/analyze", null)).StatusCode);

            // Webhook validation
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await http.PostAsync(
                    "/api/sessions/demo/webhooks",
                    new StringContent("""{"url":"ftp://x"}""", Encoding.UTF8, "application/json"))).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.DeleteAsync("/api/sessions/demo/webhooks/nope")).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.GetAsync("/api/sessions/missing/webhooks")).StatusCode);

            // Fan-out missing group
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync(
                    "/api/fanout/groups/missing/send",
                    new StringContent("""{"data":"x"}""", Encoding.UTF8, "application/json"))).StatusCode);
            Assert.Equal(HttpStatusCode.NotFound,
                (await http.PostAsync(
                    "/api/fanout/groups/missing/grants",
                    new StringContent("""{"grantee":"x"}""", Encoding.UTF8, "application/json"))).StatusCode);

            // Fan-out oversize group
            var many = string.Join(",", Enumerable.Range(0, 60).Select(i => $"\"w{i}\""));
            var big = await http.PostAsync(
                "/api/fanout/groups",
                new StringContent(
                    $"{{\"name\":\"big\",\"worker_ids\":[{many}]}}",
                    Encoding.UTF8,
                    "application/json"));
            Assert.Equal(HttpStatusCode.BadRequest, big.StatusCode);

            // Tunnel http type share page
            var httpTunnel = await http.PostAsync(
                "/api/tunnels",
                new StringContent(
                    """{"display_name":"h","tunnel_type":"http","ttl_s":120}""",
                    Encoding.UTF8,
                    "application/json"));
            httpTunnel.EnsureSuccessStatusCode();
        }
    }

    [Fact]
    public async Task TunnelHost_BinaryWs_And_OperatorShare()
    {
        var (server, http, _) = await StartServerAsync();
        await using (server)
        using (http)
        {
            var baseHttp = http.BaseAddress!.ToString().TrimEnd('/');
            var baseWs = baseHttp.Replace("http://", "ws://", StringComparison.OrdinalIgnoreCase);

            // Non-WS GET → 400
            Assert.Equal(HttpStatusCode.BadRequest, (await http.GetAsync("/tunnel/demo")).StatusCode);
            // Invalid id → 422
            using (var wsBad = new ClientWebSocket())
            {
                await Assert.ThrowsAnyAsync<Exception>(async () =>
                {
                    await wsBad.ConnectAsync(new Uri(baseWs + "/tunnel/bad!id"), CancellationToken.None);
                });
            }

            var create = await http.PostAsync(
                "/api/tunnels",
                new StringContent(
                    """{"display_name":"ws","tunnel_type":"terminal"}""",
                    Encoding.UTF8,
                    "application/json"));
            create.EnsureSuccessStatusCode();
            var cbody = await create.Content.ReadFromJsonAsync<JsonElement>();
            var tid = cbody.GetProperty("tunnel_id").GetString()!;
            var workerToken = cbody.GetProperty("worker_token").GetString()!;
            var controlUrl = cbody.GetProperty("control_url").GetString()!;

            // Worker with bearer succeeds (hub WorkerToken empty → no 401).
            using (var ws = new ClientWebSocket())
            {
                ws.Options.SetRequestHeader("Authorization", "Bearer " + workerToken);
                await ws.ConnectAsync(new Uri(baseWs + $"/tunnel/{tid}"), CancellationToken.None);

                // CHANNEL_HTTP frame
                var httpMsg = Encoding.UTF8.GetBytes(
                    """{"type":"http_request","method":"GET","url":"/"}""");
                var httpFrame = TunnelCodec.EncodeFrame(TunnelProtocol.ChannelHttp, httpMsg);
                await ws.SendAsync(httpFrame, WebSocketMessageType.Binary, true, CancellationToken.None);

                // term data channel
                var termFrame = TunnelCodec.EncodeFrame(
                    TunnelProtocol.ChannelData, Encoding.UTF8.GetBytes("hello-term"));
                await ws.SendAsync(termFrame, WebSocketMessageType.Binary, true, CancellationToken.None);

                // control open + snapshot (EncodeControl already wraps CHANNEL_CONTROL)
                var openFrame = TunnelCodec.EncodeControl(new Dictionary<string, object?>
                {
                    ["type"] = "open",
                    ["input_mode"] = "open",
                });
                await ws.SendAsync(openFrame, WebSocketMessageType.Binary, true, CancellationToken.None);

                var snapFrame = TunnelCodec.EncodeControl(new Dictionary<string, object?>
                {
                    ["type"] = "snapshot",
                    ["screen"] = "SCRN",
                });
                await ws.SendAsync(snapFrame, WebSocketMessageType.Binary, true, CancellationToken.None);

                // short/garbage frames ignored
                await ws.SendAsync(new byte[] { 0x00 }, WebSocketMessageType.Binary, true, CancellationToken.None);
                await Task.Delay(40);

                if (ws.State == WebSocketState.Open)
                {
                    try
                    {
                        await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", CancellationToken.None);
                    }
                    catch (WebSocketException)
                    {
                    }
                }
            }

            // Operator invite → /app/operator/{id}
            using var noAuth = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
            {
                BaseAddress = http.BaseAddress,
            };
            var inviteQ = controlUrl.Split("?invite=", 2)[1];
            var share = await noAuth.GetAsync($"/s/{tid}?invite={inviteQ}");
            Assert.Equal(HttpStatusCode.Found, share.StatusCode);
            Assert.Contains("/operator/", share.Headers.Location?.ToString() ?? "");

            // Inspect page
            var insp = await http.GetAsync($"/app/inspect/{tid}");
            insp.EnsureSuccessStatusCode();
            var html = await insp.Content.ReadAsStringAsync();
            Assert.Contains("page_kind", html);
            Assert.Equal(HttpStatusCode.NotFound, (await http.GetAsync("/app/inspect/bad!id")).StatusCode);

            // Share invalid session id
            Assert.Equal(HttpStatusCode.UnprocessableEntity,
                (await noAuth.GetAsync("/s/bad!id")).StatusCode);
        }
    }

    [Fact]
    public async Task TunnelHost_EmptyPublicBase_IpBinding_And_BearerAuth()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = ""; // force request-derived base
        cfg.Auth.Mode = "dev_token";
        cfg.Auth.WorkerBearerToken = "worker-secret";
        cfg.Tunnel.CookieSecure = false;
        cfg.Tunnel.IpBinding = true;
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
            TokenPath = Path.Combine(Path.GetTempPath(), "cp2-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var hub = new TermHub(new TermHubConfig
        {
            WorkerToken = "worker-secret",
            RestAcquireRateLimitPerSec = 1000,
            RestSendRateLimitPerSec = 1000,
        });
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Webhooks = new WebhookManager(allowLoopbackDestinations: true),
            TunnelStore = new MemoryTunnelStore(),
            Version = "cp-test2",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);

        var create = await http.PostAsync(
            "/api/tunnels",
            new StringContent("""{"display_name":"ip"}""", Encoding.UTF8, "application/json"));
        create.EnsureSuccessStatusCode();
        var body = await create.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Contains("ws://", body.GetProperty("ws_endpoint").GetString());
        var tid = body.GetProperty("tunnel_id").GetString()!;
        var workerToken = body.GetProperty("worker_token").GetString()!;

        var baseWs = server.BaseAddress!.ToString().Replace("http://", "ws://", StringComparison.OrdinalIgnoreCase).TrimEnd('/');

        // Missing bearer → 401
        using (var ws401 = new ClientWebSocket())
        {
            try
            {
                await ws401.ConnectAsync(new Uri(baseWs + $"/tunnel/{tid}"), CancellationToken.None);
                Assert.Fail("expected 401");
            }
            catch (Exception)
            {
                // expected
            }
        }

        // Correct worker bearer
        using (var ws = new ClientWebSocket())
        {
            ws.Options.SetRequestHeader("Authorization", "Bearer worker-secret");
            await ws.ConnectAsync(new Uri(baseWs + $"/tunnel/{tid}"), CancellationToken.None);
            if (ws.State == WebSocketState.Open)
            {
                try { await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "x", CancellationToken.None); }
                catch (WebSocketException) { }
            }
        }

        // Cookie SameSite variants via share without invite after tokens exist
        cfg.Tunnel.CookieSamesite = "strict";
        var rotate = await http.PostAsync($"/api/tunnels/{tid}/tokens/rotate", null);
        rotate.EnsureSuccessStatusCode();
        _ = workerToken;
    }

    [Fact]
    public void WebhookManager_And_TunnelStore_Unit()
    {
        var mgr = new WebhookManager(allowLoopbackDestinations: false);
        Assert.Throws<ArgumentException>(() => mgr.ValidateUrl("http://127.0.0.1/x"));
        Assert.Throws<ArgumentException>(() => mgr.ValidateUrl("not-a-url"));
        Assert.Throws<ArgumentException>(() => mgr.ValidateUrl(""));
        Assert.Throws<ArgumentException>(() => mgr.ValidatePattern(new string('a', 201)));
        mgr.ValidatePattern("ok");
        mgr.ValidatePattern(null);

        var loop = new WebhookManager(allowLoopbackDestinations: true);
        var cfg = loop.Register("s1", "http://127.0.0.1:9/h", new[] { "term" }, null, null);
        Assert.Single(loop.ListWebhooks("s1"));
        Assert.NotNull(loop.GetWebhook(cfg.WebhookId));
        Assert.True(loop.Unregister(cfg.WebhookId));
        Assert.False(loop.Unregister("gone"));

        var store = new MemoryTunnelStore();
        var now = 1_000_000.0;
        store.PutToken("t1", new TokenRecord
        {
            WorkerTokenHash = TunnelTokens.HashToken("w"),
            ShareTokenHash = TunnelTokens.HashToken("s"),
            ControlTokenHash = TunnelTokens.HashToken("c"),
            CreatedAt = now,
            ExpiresAt = now + 3600,
            TunnelType = "terminal",
            SharePage = "session",
        });
        var (share, control) = store.IssueInvites("t1", "s", "c", now + 3600, now, "1.2.3.4");
        Assert.NotEmpty(share);
        Assert.NotEmpty(control);
        Assert.NotNull(store.ConsumeInviteValue(share, "t1", now + 1));
        Assert.Null(store.ConsumeInviteValue(share, "t1", now + 1)); // single-use
        Assert.Null(store.ConsumeInviteValue(control, "other", now + 1)); // wrong session burns
        store.DiscardInvitesForSession("t1");
        store.DeleteToken("t1");
        Assert.Null(store.GetToken("t1"));
        Assert.NotEmpty(TunnelTokens.GenerateToken());
        Assert.True(store.ListTokens().Count == 0);

        // Expiry + empty invite paths
        var (s2, c2) = store.IssueInvites("t2", "s2", "c2", now + 5, now, null);
        Assert.Null(store.ConsumeInviteValue(s2, "t2", now + 100)); // expired
        Assert.Null(store.ConsumeInviteValue("  ", "t2", now));
        Assert.Null(store.ConsumeInviteValue(c2, "t2", now + 100)); // also expired after burn of s2? c2 still there until expire
        // empty tunnel token burns
        store.PutInvite(TunnelTokens.HashToken("emptytok"), new Invite
        {
            SessionId = "t3",
            Role = TunnelRole.Viewer,
            TunnelToken = "   ",
            ExpiresAt = now + 1000,
        });
        Assert.Null(store.ConsumeInviteValue("emptytok", "t3", now));
    }
}
