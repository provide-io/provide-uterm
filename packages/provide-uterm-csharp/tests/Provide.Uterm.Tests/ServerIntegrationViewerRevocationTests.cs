//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Headers;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// POST /api/sessions/{id}/viewers/revoke against live sockets: share viewers are
/// closed with 4410 "viewer access revoked" and their tokens refused from then
/// on; admins and ordinary viewers keep watching. Port of
/// tests/server/test_viewer_revocation.py's end-to-end half.
/// </summary>
public sealed class ServerIntegrationViewerRevocationTests
{
    private sealed class HeaderAuthenticator : IAuthenticator
    {
        public Task<Principal> AuthenticateAsync(AuthRequest request, CancellationToken cancellationToken = default)
        {
            var subject = request.Header("X-Test-Subject");
            if (subject.Length == 0) return Task.FromResult(Principal.Anonymous());
            var scope = request.Header("X-Test-Admin-Scope");
            var p = new Principal
            {
                SubjectId = subject,
                Roles = StringSet.Of(request.Header("X-Test-Role")),
                Scopes = StringSet.Of("*"),
                AdminSessionScope = scope.Length == 0 ? null : scope,
            };
            p.Claims["iat"] = double.Parse(request.Header("X-Test-Iat"), System.Globalization.CultureInfo.InvariantCulture);
            return Task.FromResult(p);
        }
    }

    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    internal static async Task<(UtermServer Server, int Port)> BootAsync() =>
        await BootAsync(new AuthorizationService());

    internal static async Task<(UtermServer Server, int Port)> BootAsync(AuthorizationService authz)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "bb",
            DisplayName = "BB",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "owner1",
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        hub.Registry.Put("bb", new WorkerTermState());
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new HeaderAuthenticator(),
            Authz = authz,
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "revoke",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, port);
    }

    internal static void Identify(Action<string, string> set, string subject, string role, double iat)
    {
        set("X-Test-Subject", subject);
        set("X-Test-Role", role);
        set("X-Test-Iat", iat.ToString(System.Globalization.CultureInfo.InvariantCulture));
    }

    internal static async Task<ClientWebSocket> ConnectAsync(int port, string subject, string role, double iat)
    {
        var ws = new ClientWebSocket();
        Identify(ws.Options.SetRequestHeader, subject, role, iat);
        await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/browser/bb/term"), CancellationToken.None);
        return ws;
    }

    /// <summary>Read until a text message containing <paramref name="needle"/>, or the close.</summary>
    internal static async Task<string?> ReadUntilAsync(ClientWebSocket ws, string needle)
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var buffer = new byte[64 * 1024];
        while (true)
        {
            var text = new StringBuilder();
            WebSocketReceiveResult result;
            do
            {
                result = await ws.ReceiveAsync(buffer, cts.Token);
                if (result.MessageType == WebSocketMessageType.Close) return null;
                text.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
            }
            while (!result.EndOfMessage);
            if (text.ToString().Contains(needle, StringComparison.Ordinal)) return text.ToString();
        }
    }

    private static async Task<HttpResponseMessage> PostRevokeAsync(
        int port, string subject, string role, string session = "bb", string? scope = null)
    {
        using var http = new HttpClient();
        using var req = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{port}/api/sessions/{session}/viewers/revoke");
        Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), subject, role, 100);
        if (scope is not null) req.Headers.TryAddWithoutValidation("X-Test-Admin-Scope", scope);
        return await http.SendAsync(req);
    }

    [Fact]
    public async Task Revoking_closes_share_watchers_and_refuses_their_tokens()
    {
        var (server, port) = await BootAsync();
        try
        {
            using var watcher = await ConnectAsync(port, "share:bb:blackbetty:7", "viewer", 100);
            using var admin = await ConnectAsync(port, "admin1", "admin", 100);
            using var plain = await ConnectAsync(port, "alice", "viewer", 100);
            Assert.NotNull(await ReadUntilAsync(watcher, "\"hello\""));
            Assert.NotNull(await ReadUntilAsync(admin, "\"hello\""));
            Assert.NotNull(await ReadUntilAsync(plain, "\"hello\""));

            Assert.Equal(HttpStatusCode.Forbidden, (await PostRevokeAsync(port, "share:bb:blackbetty:7", "viewer")).StatusCode);
            Assert.Equal(HttpStatusCode.Forbidden, (await PostRevokeAsync(port, "alice", "viewer")).StatusCode);

            var response = await PostRevokeAsync(port, "owner1", "operator");
            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            using var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            var root = body.RootElement;
            Assert.Equal("bb", root.GetProperty("session_id").GetString());
            Assert.Equal(1, root.GetProperty("closed").GetInt32());
            Assert.Equal(4410, root.GetProperty("close_code").GetInt32());
            Assert.Equal("viewer access revoked", root.GetProperty("close_reason").GetString());
            var revokedAt = root.GetProperty("revoked_at").GetDouble();
            Assert.True(revokedAt > 100);

            Assert.Null(await ReadUntilAsync(watcher, "never-sent"));
            Assert.Equal((WebSocketCloseStatus)4410, watcher.CloseStatus);
            Assert.Equal("viewer access revoked", watcher.CloseStatusDescription);

            foreach (var keeper in new[] { admin, plain })
            {
                var ping = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
                    new Dictionary<string, object?> { ["type"] = "ping" }));
                await keeper.SendAsync(ping, WebSocketMessageType.Text, true, CancellationToken.None);
                Assert.NotNull(await ReadUntilAsync(keeper, "\"pong\""));
            }

            // The revoked token can neither reconnect nor read.
            var refused = await Assert.ThrowsAsync<WebSocketException>(
                () => ConnectAsync(port, "share:bb:blackbetty:7", "viewer", 100));
            Assert.Contains("403", refused.Message, StringComparison.Ordinal);

            // A token issued after the cutoff watches again.
            using var fresh = await ConnectAsync(port, "share:bb:blackbetty:7", "viewer", revokedAt + 1);
            Assert.NotNull(await ReadUntilAsync(fresh, "\"hello\""));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task Revoking_an_unknown_session_is_a_404_and_a_quiet_one_closes_nothing()
    {
        var (server, port) = await BootAsync();
        try
        {
            Assert.Equal(HttpStatusCode.NotFound, (await PostRevokeAsync(port, "admin1", "admin", "nope")).StatusCode);
            var response = await PostRevokeAsync(port, "admin1", "admin");
            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            using var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Assert.Equal(0, body.RootElement.GetProperty("closed").GetInt32());
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_socket_registered_after_the_revoke_scan_is_closed_on_registration()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        UtermServer.BrowserPreRegisterHook = id => authz.RevokeShareViewers(id, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0);
        try
        {
            using var watcher = await ConnectAsync(port, "share:bb:blackbetty:7", "viewer", 100);
            Assert.Null(await ReadUntilAsync(watcher, "never-sent"));
            Assert.Equal((WebSocketCloseStatus)4410, watcher.CloseStatus);
            Assert.Equal("viewer access revoked", watcher.CloseStatusDescription);

            // An admin registering after a revoke is untouched.
            using var admin = await ConnectAsync(port, "admin1", "admin", 100);
            Assert.NotNull(await ReadUntilAsync(admin, "\"hello\""));
        }
        finally
        {
            UtermServer.BrowserPreRegisterHook = static _ => { };
            await server.StopAsync();
        }
    }

    private static async Task<(HttpResponseMessage Response, Stream Body)> OpenStreamAsync(
        HttpClient http, int port, string subject, string role)
    {
        var req = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}/api/sessions/bb/events/stream");
        Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), subject, role, 100);
        var response = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead);
        return (response, await response.Content.ReadAsStreamAsync());
    }

    [Fact]
    public async Task Revoking_ends_a_live_share_viewer_event_stream()
    {
        var (server, port) = await BootAsync();
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            var (watcherResponse, watcherBody) = await OpenStreamAsync(http, port, "share:bb:blackbetty:7", "viewer");
            using var _w = watcherResponse;
            var (plainResponse, _) = await OpenStreamAsync(http, port, "alice", "viewer");
            using var _p = plainResponse;
            Assert.Equal(HttpStatusCode.OK, watcherResponse.StatusCode);
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while (server.ViewerStreamCount("bb") != 1 && DateTime.UtcNow < deadline) await Task.Delay(20);
            Assert.Equal(1, server.ViewerStreamCount("bb"));

            var response = await PostRevokeAsync(port, "admin1", "admin");
            using var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Assert.Equal(1, body.RootElement.GetProperty("closed").GetInt32());

            using var reader = new StreamReader(watcherBody);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            var all = await reader.ReadToEndAsync(cts.Token);
            Assert.EndsWith("data: {\"type\":\"viewer_access_revoked\",\"reason\":\"viewer access revoked\"}\n\n", all);
            Assert.Equal(0, server.ViewerStreamCount("bb"));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_share_viewer_stream_opened_after_the_cutoff_ends_at_once()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        UtermServer.SseOpenHook = id => authz.RevokeShareViewers(id, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0);
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            var (response, stream) = await OpenStreamAsync(http, port, "share:bb:blackbetty:7", "viewer");
            using var _r = response;
            using var reader = new StreamReader(stream);
            Assert.Equal(
                "data: {\"type\":\"viewer_access_revoked\",\"reason\":\"viewer access revoked\"}\n\n",
                await reader.ReadToEndAsync());
        }
        finally
        {
            UtermServer.SseOpenHook = static _ => { };
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task Other_owners_and_admins_scoped_elsewhere_cannot_revoke()
    {
        var (server, port) = await BootAsync();
        try
        {
            Assert.Equal(HttpStatusCode.Forbidden, (await PostRevokeAsync(port, "owner2", "operator")).StatusCode);
            Assert.Equal(HttpStatusCode.Forbidden, (await PostRevokeAsync(port, "scoped", "admin", scope: "elsewhere")).StatusCode);
            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "scoped", "admin", scope: "bb")).StatusCode);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task The_owner_and_the_hijack_holder_keep_their_sockets_through_a_revoke()
    {
        var (server, port) = await BootAsync();
        try
        {
            using var worker = new ClientWebSocket();
            await worker.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/worker/bb/term"), CancellationToken.None);
            _ = Task.Run(async () =>
            {
                var sink = new byte[65536];
                try
                {
                    while (worker.State == WebSocketState.Open) await worker.ReceiveAsync(sink, CancellationToken.None);
                }
                catch (WebSocketException)
                {
                }
            });
            using var holder = await ConnectAsync(port, "admin1", "admin", 100);
            using var owner = await ConnectAsync(port, "owner1", "operator", 100);
            Assert.NotNull(await ReadUntilAsync(holder, "\"hello\""));
            Assert.Contains("\"role\":\"operator\"", await ReadUntilAsync(owner, "\"hello\""));

            async Task SendAsync(ClientWebSocket ws, string type) =>
                await ws.SendAsync(
                    Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
                        new Dictionary<string, object?> { ["type"] = type })),
                    WebSocketMessageType.Text, true, CancellationToken.None);

            await SendAsync(holder, "hijack_request");
            Assert.NotNull(await ReadUntilAsync(holder, "\"owner\":\"me\""));

            var response = await PostRevokeAsync(port, "owner1", "operator");
            using var body = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Assert.Equal(0, body.RootElement.GetProperty("closed").GetInt32());
            foreach (var keeper in new[] { holder, owner })
            {
                await SendAsync(keeper, "ping");
                Assert.NotNull(await ReadUntilAsync(keeper, "\"pong\""));
            }
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task Deleting_a_session_forgets_its_cutoff()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "admin1", "admin")).StatusCode);
            Assert.NotNull(authz.ShareViewerRevocation("bb"));
            using var http = new HttpClient();
            using var req = new HttpRequestMessage(HttpMethod.Delete, $"http://127.0.0.1:{port}/api/sessions/bb");
            Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), "admin1", "admin", 100);
            Assert.Equal(HttpStatusCode.OK, (await http.SendAsync(req)).StatusCode);
            Assert.Null(authz.ShareViewerRevocation("bb"));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_full_cutoff_table_answers_429()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            for (var i = 0; i < AuthorizationService.ShareViewerRevocationsMax; i++)
            {
                authz.RevokeShareViewers("other-" + i, now);
            }
            var response = await PostRevokeAsync(port, "admin1", "admin");
            Assert.Equal((HttpStatusCode)429, response.StatusCode);
            Assert.Contains("too many sessions with revoked viewers", await response.Content.ReadAsStringAsync());
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task An_owners_revokes_are_capped_per_principal_but_a_global_admins_are_not()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        try
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            for (var i = 0; i < AuthorizationService.ShareViewerRevocationsPerPrincipalMax; i++)
            {
                authz.RevokeShareViewers("mine-" + i, now, "owner1");
            }
            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "admin1", "admin")).StatusCode);
            Assert.NotNull(authz.ShareViewerRevocation("mine-0"));
            // A re-revoke would leave bb with the admin, uncapped: start afresh.
            authz.ForgetShareViewers("bb");
            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "owner1", "operator")).StatusCode);
            Assert.Null(authz.ShareViewerRevocation("mine-0"));
            Assert.NotNull(authz.ShareViewerRevocation("bb"));
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_cutoff_the_cap_evicts_is_audited_by_session_and_principal()
    {
        var authz = new AuthorizationService();
        var (server, port) = await BootAsync(authz);
        var records = new List<IReadOnlyDictionary<string, object?>>();
        UtermServer.ViewersAudit = fields =>
        {
            lock (records) records.Add(fields);
        };
        try
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            for (var i = 0; i < AuthorizationService.ShareViewerRevocationsPerPrincipalMax; i++)
            {
                authz.RevokeShareViewers("mine-" + i, now, "owner1");
            }
            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "owner1", "operator")).StatusCode);
            var record = Assert.Single(records);
            Assert.Equal("session.viewers.cutoff_evicted", record["action"]);
            Assert.Equal("owner1", record["principal"]);
            Assert.Equal("mine-0", record["session_id"]);
            Assert.Equal("bb", record["revoked_session_id"]);
        }
        finally
        {
            UtermServer.ViewersAudit = static _ => { };
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_long_poll_in_flight_when_its_viewer_is_revoked_answers_403()
    {
        var (server, port) = await BootAsync();
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            using var req = new HttpRequestMessage(
                HttpMethod.Get, $"http://127.0.0.1:{port}/api/sessions/bb/events/watch?timeout_ms=1500");
            Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), "share:bb:blackbetty:7", "viewer", 100);
            var poll = http.SendAsync(req);
            await Task.Delay(300);
            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "admin1", "admin")).StatusCode);
            var answered = await poll;
            Assert.Equal(HttpStatusCode.Forbidden, answered.StatusCode);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task A_revoked_socket_that_ignores_the_close_is_aborted_after_the_grace()
    {
        var (server, port) = await BootAsync();
        var previous = UtermServer.ViewerCloseGrace;
        UtermServer.ViewerCloseGrace = TimeSpan.FromMilliseconds(200);
        try
        {
            // Never read: the client cannot answer the close handshake.
            using var silent = await ConnectAsync(port, "share:bb:blackbetty:7", "viewer", 100);
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while (server.HubBrowserCount("bb") != 1 && DateTime.UtcNow < deadline) await Task.Delay(20);
            Assert.Equal(1, server.HubBrowserCount("bb"));

            Assert.Equal(HttpStatusCode.OK, (await PostRevokeAsync(port, "admin1", "admin")).StatusCode);
            deadline = DateTime.UtcNow.AddSeconds(5);
            while (server.HubBrowserCount("bb") != 0 && DateTime.UtcNow < deadline) await Task.Delay(20);
            Assert.Equal(0, server.HubBrowserCount("bb"));
        }
        finally
        {
            UtermServer.ViewerCloseGrace = previous;
            await server.StopAsync();
        }
    }
}
