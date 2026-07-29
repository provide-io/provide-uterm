//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Net.Http.Json;
using System.Security.Claims;
using System.Text;
using System.Text.Json;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// A configured session can be hijacked, because starting it attaches a worker.
///
/// The reference's <c>HostedSessionRuntime</c> is a worker: it starts the
/// session's connector and connects it to the hub over
/// <c>/ws/worker/{id}/term</c> (<c>server/runtime.py: _run</c> /
/// <c>_bridge_session</c>), which is why
/// <c>POST /worker/{id}/hijack/acquire</c> mints a lease against a session
/// nobody attached by hand. A port that starts the connector but never presents
/// it to the hub answers <c>409 No worker connected for this session.</c> to the one sequence
/// every operator runs first.
///
/// The sequence asserted here is the one verified by hand against a live
/// reference server: mode → acquire → snapshot → acquire again → release.
/// </summary>
public sealed class SessionHijackParityTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    /// <summary>
    /// A token for a principal the server will authenticate but not privilege —
    /// the other half of the 401/403 distinction. <see cref="DevIdp.Setup"/>
    /// rewrites the config to jwt mode with a symmetric secret, so a second
    /// token signed with that secret is a real, verifiable credential.
    /// </summary>
    private static string MintToken(AuthConfig auth, string subject, params string[] roles)
    {
        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(auth.JwtPublicKeyPem!));
        var claims = new List<Claim> { new("sub", subject) };
        foreach (var role in roles)
        {
            claims.Add(new Claim(auth.JwtRolesClaim, role));
        }

        var now = DateTimeOffset.UtcNow;
        return new JwtSecurityTokenHandler().WriteToken(new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddHours(1).UtcDateTime,
            signingCredentials: new SigningCredentials(key, SecurityAlgorithms.HmacSha256)));
    }

    private static async Task<(UtermServer Server, HttpClient Http, AuthConfig Auth)> StartAsync()
    {
        var cfg = UtermServerConfig.Default();
        var port = FreePort();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "hijack-parity-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig { RestAcquireRateLimitPerSec = 1000, RestSendRateLimitPerSec = 1000 }),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "hijack-parity",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return (server, http, cfg.Auth);
    }

    private static StringContent Json(string body) => new(body, Encoding.UTF8, "application/json");

    private static async Task<HttpResponseMessage> SwitchToHijackMode(HttpClient http) =>
        await http.PostAsync("/api/sessions/provide-shell/mode", Json("""{"input_mode": "hijack"}"""));

    [Fact]
    public async Task An_Auto_Started_Session_Presents_A_Worker_The_Hijack_Routes_Can_Lease()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();

            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.OK, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            Assert.True(body.GetProperty("ok").GetBoolean());
            Assert.False(string.IsNullOrEmpty(body.GetProperty("hijack_id").GetString()));
            Assert.Equal("tester", body.GetProperty("owner").GetString());
        }
    }

    [Fact]
    public async Task The_Lease_Reads_The_Running_Connectors_Own_Screen()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            var hijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>())
                .GetProperty("hijack_id").GetString();

            var snapshot = await http.GetAsync($"/worker/provide-shell/hijack/{hijackId}/snapshot");

            Assert.Equal(HttpStatusCode.OK, snapshot.StatusCode);
            var body = await snapshot.Content.ReadFromJsonAsync<JsonElement>();
            var screen = body.GetProperty("snapshot").GetProperty("screen").GetString();
            // The connector's own screen, not the empty placeholder the route
            // falls back to when no worker ever reported one.
            Assert.Contains("provide-shell", screen!, StringComparison.Ordinal);
        }
    }

    [Fact]
    public async Task A_Second_Acquire_Is_Refused_While_The_Lease_Is_Held()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            await http.PostAsync("/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            var second = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "other", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.Conflict, second.StatusCode);
            var body = await second.Content.ReadFromJsonAsync<JsonElement>();
            // The reference's lease refusal is the error key and nothing else
            // (bridge/routes/rest.py:218). An `ok: false` alongside it is a
            // second envelope for clients to learn.
            Assert.False(body.TryGetProperty("ok", out _));
            Assert.Equal("Worker is already hijacked.", body.GetProperty("error").GetString());
        }
    }

    [Fact]
    public async Task Releasing_Hands_The_Session_Back()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            var hijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>())
                .GetProperty("hijack_id").GetString();

            var release = await http.PostAsync($"/worker/provide-shell/hijack/{hijackId}/release", Json("{}"));

            Assert.Equal(HttpStatusCode.OK, release.StatusCode);
            Assert.True((await release.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("ok").GetBoolean());
            // And the session can be leased again, which is the point of releasing.
            var again = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            Assert.Equal(HttpStatusCode.OK, again.StatusCode);
        }
    }

    [Fact]
    public async Task Keys_Sent_Under_The_Lease_Reach_The_Running_Connector()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            var hijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>())
                .GetProperty("hijack_id").GetString();

            var send = await http.PostAsync(
                $"/worker/provide-shell/hijack/{hijackId}/send", Json("""{"keys": "help"}"""));

            Assert.Equal(HttpStatusCode.OK, send.StatusCode);
            var snapshot = await http.GetAsync($"/worker/provide-shell/hijack/{hijackId}/snapshot");
            var body = await snapshot.Content.ReadFromJsonAsync<JsonElement>();
            var screen = body.GetProperty("snapshot").GetProperty("screen").GetString();
            // The connector echoed the keys onto its line, so the screen the
            // lease reads back is the one the worker actually holds.
            Assert.Contains("help", screen!, StringComparison.Ordinal);
        }
    }

    /// <summary>
    /// Stopping the session takes its worker away again. The reference's runtime
    /// stops the connector and its worker socket goes with it; a port that left
    /// the worker attached would let a lease be taken on a stopped session.
    /// </summary>
    [Fact]
    public async Task A_Stopped_Session_Stops_Presenting_A_Worker()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            (await http.PostAsync("/api/sessions/provide-shell/disconnect", null)).EnsureSuccessStatusCode();

            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.Conflict, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            // The reference's own words for this arm (bridge/routes/rest.py:214).
            Assert.Equal("No worker connected for this session.", body.GetProperty("error").GetString());
            Assert.False(body.TryGetProperty("ok", out _));

            // And restarting brings both back, so the session is usable again.
            (await http.PostAsync("/api/sessions/provide-shell/restart", null)).EnsureSuccessStatusCode();
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            var afterRestart = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            Assert.Equal(HttpStatusCode.OK, afterRestart.StatusCode);
        }
    }

    /// <summary>
    /// A session left in the configuration's <c>open</c> mode is still refused —
    /// the reference's <c>open_mode</c> arm, not <c>no_worker</c>. Attaching a
    /// worker must not turn the mode gate off.
    /// </summary>
    [Fact]
    public async Task Open_Mode_Is_Still_Refused_For_Its_Own_Reason()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.Conflict, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            // The reference names the reason, because the fix is a mode change
            // (bridge/routes/rest.py:216).
            Assert.Equal("Hijack not available in open input mode.", body.GetProperty("error").GetString());
            Assert.False(body.TryGetProperty("ok", out _));
        }
    }

    /// <summary>
    /// A worker nobody registered is absent, not conflicting — and it is refused
    /// in the <c>detail</c> envelope, by the authorization layer that runs before
    /// the lease routes ever see the request (<c>app/hub_authz.py:110</c>).
    /// The reference calls it a session even on a worker route; that wording is
    /// the contract, not a slip.
    /// </summary>
    [Fact]
    public async Task A_Worker_That_Does_Not_Exist_Is_Absent_Not_Conflicting()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            var acquire = await http.PostAsync(
                "/worker/no-such-worker/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.NotFound, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("unknown session: no-such-worker", body.GetProperty("detail").GetString());
            Assert.False(body.TryGetProperty("error", out _));
            Assert.False(body.TryGetProperty("ok", out _));
        }
    }

    /// <summary>
    /// A lease released twice is gone, and the second refusal carries the lease
    /// routes' <c>error</c> envelope with nothing beside it
    /// (<c>bridge/routes/rest.py:442</c>).
    /// </summary>
    [Fact]
    public async Task Releasing_An_Already_Released_Lease_Says_Only_That_It_Is_Gone()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            var hijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>())
                .GetProperty("hijack_id").GetString();
            (await http.PostAsync($"/worker/provide-shell/hijack/{hijackId}/release", Json("{}")))
                .EnsureSuccessStatusCode();

            var again = await http.PostAsync($"/worker/provide-shell/hijack/{hijackId}/release", Json("{}"));

            Assert.Equal(HttpStatusCode.NotFound, again.StatusCode);
            var body = await again.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("Invalid or expired hijack session.", body.GetProperty("error").GetString());
            Assert.False(body.TryGetProperty("ok", out _));
        }
    }

    /// <summary>
    /// A caller who presented no credential is nobody, and the reference tells
    /// them so before it consults any session state
    /// (<c>app/factory_impl.py:269</c>, the dependency the whole hub router is
    /// mounted behind). Answering 403 instead would say the caller was
    /// identified and found wanting — and would make the refusal depend on
    /// state an unauthenticated caller must not be able to probe.
    /// </summary>
    [Fact]
    public async Task An_Unauthenticated_Acquire_Is_Told_It_Is_Nobody()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        using (http)
        using (var anon = new HttpClient { BaseAddress = http.BaseAddress })
        {
            var acquire = await anon.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "nobody", "lease_s": 60}"""));

            Assert.Equal(HttpStatusCode.Unauthorized, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("authentication required", body.GetProperty("detail").GetString());
        }
    }

    /// <summary>
    /// The other half of that distinction: a caller the server did authenticate,
    /// holding a role without <c>session.control.hijack</c>, still gets 403.
    /// Fixing the anonymous case must not collapse these two into one answer.
    /// </summary>
    [Fact]
    public async Task An_Authenticated_Caller_Without_The_Role_Is_Still_Forbidden()
    {
        var (server, http, auth) = await StartAsync();
        await using (server)
        using (http)
        using (var viewer = new HttpClient { BaseAddress = http.BaseAddress })
        {
            viewer.DefaultRequestHeaders.TryAddWithoutValidation(
                "Authorization", "Bearer " + MintToken(auth, "watcher", "viewer"));

            var acquire = await viewer.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "watcher", "lease_s": 60}"""));

            Assert.Equal(HttpStatusCode.Forbidden, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("insufficient privileges", body.GetProperty("detail").GetString());
        }
    }
}
