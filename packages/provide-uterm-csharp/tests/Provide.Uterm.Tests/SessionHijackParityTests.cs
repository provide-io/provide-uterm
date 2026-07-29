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

/// <summary>
/// A configured session can be hijacked, because starting it attaches a worker.
///
/// The reference's <c>HostedSessionRuntime</c> is a worker: it starts the
/// session's connector and connects it to the hub over
/// <c>/ws/worker/{id}/term</c> (<c>server/runtime.py: _run</c> /
/// <c>_bridge_session</c>), which is why
/// <c>POST /worker/{id}/hijack/acquire</c> mints a lease against a session
/// nobody attached by hand. A port that starts the connector but never presents
/// it to the hub answers <c>409 No worker connected.</c> to the one sequence
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

    private static async Task<(UtermServer Server, HttpClient Http)> StartAsync()
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
        return (server, http);
    }

    private static StringContent Json(string body) => new(body, Encoding.UTF8, "application/json");

    private static async Task<HttpResponseMessage> SwitchToHijackMode(HttpClient http) =>
        await http.PostAsync("/api/sessions/provide-shell/mode", Json("""{"input_mode": "hijack"}"""));

    [Fact]
    public async Task An_Auto_Started_Session_Presents_A_Worker_The_Hijack_Routes_Can_Lease()
    {
        var (server, http) = await StartAsync();
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
        var (server, http) = await StartAsync();
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
        var (server, http) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            await http.PostAsync("/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            var second = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "other", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.Conflict, second.StatusCode);
            var body = await second.Content.ReadFromJsonAsync<JsonElement>();
            Assert.False(body.GetProperty("ok").GetBoolean());
            Assert.Equal("Worker is already hijacked.", body.GetProperty("error").GetString());
        }
    }

    [Fact]
    public async Task Releasing_Hands_The_Session_Back()
    {
        var (server, http) = await StartAsync();
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
        var (server, http) = await StartAsync();
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
        var (server, http) = await StartAsync();
        await using (server)
        using (http)
        {
            (await SwitchToHijackMode(http)).EnsureSuccessStatusCode();
            (await http.PostAsync("/api/sessions/provide-shell/disconnect", null)).EnsureSuccessStatusCode();

            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.Conflict, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("No worker connected.", body.GetProperty("error").GetString());

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
        var (server, http) = await StartAsync();
        await using (server)
        using (http)
        {
            var acquire = await http.PostAsync(
                "/worker/provide-shell/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));

            Assert.Equal(HttpStatusCode.Conflict, acquire.StatusCode);
            var body = await acquire.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("Worker is in open input mode.", body.GetProperty("error").GetString());
        }
    }
}
