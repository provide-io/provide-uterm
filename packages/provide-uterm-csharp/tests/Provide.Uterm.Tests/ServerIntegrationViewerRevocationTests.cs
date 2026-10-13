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
            var p = new Principal
            {
                SubjectId = subject,
                Roles = StringSet.Of(request.Header("X-Test-Role")),
                Scopes = StringSet.Of("*"),
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

    private static async Task<(UtermServer Server, int Port)> BootAsync()
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
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "revoke",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, port);
    }

    private static void Identify(Action<string, string> set, string subject, string role, double iat)
    {
        set("X-Test-Subject", subject);
        set("X-Test-Role", role);
        set("X-Test-Iat", iat.ToString(System.Globalization.CultureInfo.InvariantCulture));
    }

    private static async Task<ClientWebSocket> ConnectAsync(int port, string subject, string role, double iat)
    {
        var ws = new ClientWebSocket();
        Identify(ws.Options.SetRequestHeader, subject, role, iat);
        await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/browser/bb/term"), CancellationToken.None);
        return ws;
    }

    /// <summary>Read until a text message containing <paramref name="needle"/>, or the close.</summary>
    private static async Task<string?> ReadUntilAsync(ClientWebSocket ws, string needle)
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

    private static async Task<HttpResponseMessage> PostRevokeAsync(int port, string subject, string role, string session = "bb")
    {
        using var http = new HttpClient();
        using var req = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{port}/api/sessions/{session}/viewers/revoke");
        Identify((k, v) => req.Headers.TryAddWithoutValidation(k, v), subject, role, 100);
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
}
