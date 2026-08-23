//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Auth;
using Provide.Uterm.Bridge;
using Provide.Uterm.Client;
using Provide.Uterm.Defaults;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.DeckMux;

namespace Provide.Uterm.Tests;

public class ClientDefaultsAuthConfigTests
{
    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, HttpResponseMessage> Responder { get; set; } =
            _ => new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"ok":true}""", Encoding.UTF8, "application/json"),
            };

        public List<HttpRequestMessage> Requests { get; } = new();

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Requests.Add(request);
            return Task.FromResult(Responder(request));
        }
    }

    [Fact]
    public async Task HijackClient_MethodSurface_WithMockHandler()
    {
        var handler = new StubHandler();
        using var http = new HttpClient(handler);
        using var client = new HijackClient("http://example.test", httpClient: http);

        Assert.Equal(HttpStatusCode.OK, (await client.HealthAsync()) is not null
            ? HttpStatusCode.OK
            : HttpStatusCode.BadRequest);

        await client.AcquireAsync("w1", "operator", 30);
        await client.HeartbeatAsync("w1", "h1", 30);
        await client.SendAsync("w1", "h1", "keys");
        await client.StepAsync("w1", "h1", 2);
        await client.SnapshotAsync("w1", "h1", 100);
        await client.EventsAsync("w1", "h1", 0, 10);
        await client.SetInputModeAsync("w1", "open");
        await client.DisconnectWorkerAsync("w1");
        await client.ReleaseAsync("w1", "h1");

        // Session APIs
        await client.ListSessionsAsync();
        await client.GetSessionAsync("s1");
        await client.SessionSnapshot("s1");
        await client.SessionEvents("s1");
        await client.WatchSessionEvents("s1");
        await client.SetSessionMode("s1", "open");
        await client.ConnectSession("s1");
        await client.DisconnectSession("s1");
        await client.QuickConnect(new Dictionary<string, object?> { ["host"] = "x" });
        await client.Post("/api/custom", new Dictionary<string, object?> { ["a"] = 1 });

        // Aliases
        await client.Acquire("w1");
        await client.Acquire("w1", new Dictionary<string, object?> { ["owner"] = "admin", ["lease_s"] = 10 });
        await client.Heartbeat("w1", "h1");
        await client.Send("w1", "h1", "x");
        await client.Step("w1", "h1");
        await client.Release("w1", "h1");
        await client.Snapshot("w1", "h1");
        await client.Snapshot("w1");
        await client.Events("w1", "h1");
        await client.Events("w1");
        await client.SetInputMode("w1", "hijack");
        await client.DisconnectWorker("w1");
        await client.Health();
        await client.ListSessions();
        await client.GetSession("s1");

        Assert.True(handler.Requests.Count >= 20);

        using var bearer = HijackClient.WithBearer("http://example.test", "tok");
        using var bearer2 = HijackClient.CreateWithBearer("http://example.test", "tok");
        Assert.NotNull(bearer);
        Assert.NotNull(bearer2);
    }

    [Fact]
    public async Task HijackClient_ErrorsAndValidation()
    {
        var handler = new StubHandler
        {
            Responder = _ => new HttpResponseMessage(HttpStatusCode.BadRequest)
            {
                Content = new StringContent("""{"error":"nope"}""", Encoding.UTF8, "application/json"),
            },
        };
        using var http = new HttpClient(handler);
        using var client = new HijackClient("http://example.test", httpClient: http);
        var ex = await Assert.ThrowsAsync<ApiException>(() => client.HealthAsync());
        Assert.Equal(400, ex.StatusCode);

        await Assert.ThrowsAsync<ArgumentException>(() => client.AcquireAsync("../x"));
        await Assert.ThrowsAsync<ArgumentException>(() => client.AcquireAsync(""));
    }

    [Fact]
    public void Defaults_Constants()
    {
        Assert.Equal(2102, TerminalDefaults.TelnetPort);
        Assert.Equal(8780, TerminalDefaults.ServerPort);
        Assert.Equal("127.0.0.1", TerminalDefaults.ServerHost);
        Assert.Equal(8765, TerminalDefaults.ProxyPort);
        Assert.Contains(".uterm", TerminalDefaults.TokenFile(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Auth_Fingerprint_And_NullResolver()
    {
        // Minimal fake "ssh-ed25519 AAAA comment" line (invalid key body is fine for prefix path if b64 ok)
        var blob = Encoding.UTF8.GetBytes("ssh-ed25519 " + Convert.ToBase64String(Encoding.UTF8.GetBytes("key-bytes")) + " user@host");
        var fp = SshAuth.FingerprintFromOpenSshBlob(blob);
        Assert.StartsWith("SHA256:", fp, StringComparison.Ordinal);

        var raw = SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes("not-a-key-line"));
        Assert.StartsWith("SHA256:", raw, StringComparison.Ordinal);

        var nullRes = new NullResolver();
        Assert.Null(await nullRes.ResolveAsync("fp", Array.Empty<byte>(), "u"));
    }

    [Fact]
    public async Task Auth_AuthorizedKeysFileResolver()
    {
        var path = Path.Combine(Path.GetTempPath(), "ak-" + Guid.NewGuid().ToString("N"));
        try
        {
            var keyBody = Convert.ToBase64String(Encoding.UTF8.GetBytes("test-key-material"));
            var line = $"ssh-ed25519 {keyBody} alice@host";
            File.WriteAllText(path, "# comment\n\n" + line + "\n");
            var fp = SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes(line));
            var resolver = new SshAuth.AuthorizedKeysFileResolver(path);
            var id = await resolver.ResolveAsync(fp, Encoding.UTF8.GetBytes(line), "alice");
            Assert.NotNull(id);
            Assert.False(string.IsNullOrEmpty(id!.Subject));
            Assert.Null(await resolver.ResolveAsync("SHA256:nope", Array.Empty<byte>(), "x"));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void ConfigLoader_Default_And_Toml()
    {
        var def = ConfigLoader.Load(null);
        Assert.Equal("dev_token", def.Auth.Mode);
        Assert.False(string.IsNullOrEmpty(def.Server.PublicBaseUrl));
        Assert.Equal("x-uterm-tenant", def.Auth.TenantHeader);
        Assert.Equal("uterm_tenant", def.Auth.TenantCookie);
        Assert.Equal("tenant_id", def.Auth.JWTTenantClaim);

        // The type alone does not distinguish the explicit existence check from
        // .NET's own FileNotFoundException on a bad read — both throw the same
        // type — so the message (which names the path) is what proves this is
        // the loader's own guard rather than an accidental fallthrough.
        var missing = Assert.Throws<FileNotFoundException>(() => ConfigLoader.Load("/no/such/server.toml"));
        Assert.Contains("/no/such/server.toml", missing.Message, StringComparison.Ordinal);

        var path = Path.Combine(Path.GetTempPath(), "sc-" + Guid.NewGuid().ToString("N") + ".toml");
        try
        {
            File.WriteAllText(path, """
                environment = "dev"
                browser_rate_limit_per_sec = 12.5
                max_workers = 50
                max_connections_per_principal = 3
                worker_frame_on_invalid = "close"

                [server]
                host = "0.0.0.0"
                port = 9999
                title = "test"
                node_id = "n1"
                allowed_origins = ["http://a"]

                [auth]
                mode = "jwt"
                jwt_issuer = "iss"
                jwt_audience = "aud"
                tenant_header = "x-t"
                tenant_cookie = "tc"
                jwt_tenant_claim = "tenant"
                api_keys_enabled = true
                trusted_proxy_ips = ["127.0.0.1"]
                jwt_algorithms = ["HS256", "RS256"]

                [control_plane]
                backend = "memory"
                database_url = "sqlite://x"

                [security]
                mode = "strict"
                metrics_require_auth = true
                default_session_visibility = "private"

                [[sessions]]
                session_id = "demo"
                display_name = "Demo"
                connector_type = "shell"
                visibility = "public"
                owner = "me"
                """);
            var cfg = ConfigLoader.Load(path);
            Assert.Equal("dev", cfg.Environment);
            Assert.Equal(12.5, cfg.BrowserRateLimitPerSec);
            Assert.Equal(50, cfg.MaxWorkers);
            Assert.Equal(9999, cfg.Server.Port);
            Assert.Equal("jwt", cfg.Auth.Mode);
            Assert.Equal("x-t", cfg.Auth.TenantHeader);
            Assert.Equal("tc", cfg.Auth.TenantCookie);
            Assert.Equal("tenant", cfg.Auth.JWTTenantClaim);
            Assert.True(cfg.Auth.ApiKeysEnabled);
            Assert.Equal("memory", cfg.ControlPlane.Backend);
            Assert.True(cfg.Security.MetricsRequireAuth);
            Assert.Single(cfg.Sessions);
            Assert.Equal("demo", cfg.Sessions[0].SessionId);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Bridge_ProtocolContracts()
    {
        var (v, ok) = ProtocolContracts.NegotiateProtocolVersion(1, 1);
        Assert.True(ok);
        Assert.Equal(1, v);
        Assert.Equal(1, ProtocolContracts.Negotiate(1, 1));
        Assert.Throws<ProtocolMismatchException>(() => ProtocolContracts.Negotiate(9, 9));

        var (min, max) = ProtocolContracts.ParseClientRange(new Dictionary<string, object?>
        {
            ["protocol"] = new Dictionary<string, object?> { ["min"] = 1L, ["max"] = 1L },
        });
        Assert.Equal(1, min);
        Assert.Equal(1, max);

        var single = ProtocolContracts.ParseClientRange(new Dictionary<string, object?>
        {
            ["protocol_version"] = 1,
        });
        Assert.Equal((1, 1), single);

        var defaults = ProtocolContracts.ParseClientRange(new Dictionary<string, object?>());
        Assert.Equal((1, 1), defaults);
    }

    [Fact]
    public void CtrlMsg_Builders_BeyondIdentity()
    {
        Assert.Equal("session_token", Builders.MakeSessionToken("tok", 3)["type"]?.ToString());
        Assert.Equal(3, Convert.ToInt32(Builders.MakeSessionToken("tok", 3)["player_id"]));
        Assert.Throws<ArgumentException>(() => Builders.MakeSessionToken(""));

        Assert.Equal("resume", Builders.MakeResume("t", 1)["type"]?.ToString());
        Assert.Throws<ArgumentException>(() => Builders.MakeResume(""));

        Assert.Equal("resume_ok", Builders.MakeResumeOk()["type"]?.ToString());
        Assert.Equal("resume_failed", Builders.MakeResumeFailed("x", includeReason: true)["type"]?.ToString());
        Assert.Equal("x", Builders.MakeResumeFailed("x", includeReason: true)["reason"]?.ToString());
        Assert.False(Builders.MakeResumeFailed().ContainsKey("reason"));

        var links = Builders.MakeLinkPatterns(
        [
            new Dictionary<string, object?> { ["pattern"] = @"\w+", ["action"] = "cmd", ["id"] = "a" },
        ]);
        Assert.Equal("link_patterns", links["type"]?.ToString());
        Assert.Throws<ArgumentException>(() => Builders.MakeLinkPatterns(
        [
            new Dictionary<string, object?> { ["pattern"] = "x", ["action"] = "nope" },
        ]));
        Assert.Throws<ArgumentException>(() => Builders.MakeLinkPatterns(
        [
            new Dictionary<string, object?> { ["action"] = "cmd" },
        ]));

        var presence = Builders.MakePresenceUpdate("u1", new Dictionary<string, object?>
        {
            ["name"] = "Alice",
            ["skip"] = null,
        });
        Assert.Equal("Alice", presence["name"]?.ToString());
        Assert.False(presence.ContainsKey("skip"));
    }

    [Fact]
    public void DeckMux_Identity_ParseAndPresence()
    {
        var frame = Builders.MakeIdentity("alice",
            claims: new Dictionary<string, object?> { ["display_name"] = "Alice", ["role"] = "admin", ["color"] = "#ff0000" },
            includeClaims: true);
        var id = Identity.ParseIdentityFrame(frame!);
        Assert.NotNull(id);
        Assert.Equal("alice", id!.Subject);

        var presence = Identity.PresenceFromIdentity(id, "conn-1");
        Assert.Equal("Alice", presence.Name);
        Assert.Equal("admin", presence.Role);
        Assert.Equal("#ff0000", presence.Color);
        Assert.Equal("AL", presence.Initials);

        var principal = Identity.IdentityAsPrincipal(id);
        Assert.Equal("alice", principal.SubjectId);
        Assert.Equal("Alice", principal.DisplayName);

        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?> { ["type"] = "other" }));
        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = 99,
            ["subject"] = "x",
        }));
    }

    [Fact]
    public void ServerFactory_CreateFromConfig()
    {
        var cfg = UtermServerConfig.Default();
        cfg.Auth.Mode = "dev_token";
        var (server, token) = ServerFactory.CreateFromConfig(cfg, "test-ver");
        Assert.NotNull(server);
        Assert.False(string.IsNullOrEmpty(token));
    }
}
