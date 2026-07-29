//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// Two things the live matrix could not see, because the one field that would
/// have shown them is declared volatile in scenario 004.
///
/// <para><b>What a session's lifecycle is called.</b> The reference's vocabulary
/// is <c>stopped | starting | running | error</c>
/// (<c>bridge/contracts.py: SessionLifecycle</c>, set by
/// <c>server/runtime.py</c>). Anything else is a name no other port and no
/// dashboard knows.</para>
///
/// <para><b>What <c>auto_start</c> does.</b> The reference brings such a session
/// up when the server starts (<c>registry.start_auto_start_sessions</c>, run
/// from the app lifespan). A port that stores the flag and never acts on it
/// answers "not running" to every client for a session the operator asked to
/// be running.</para>
///
/// <para><b>What <c>/analyze</c> answers.</b> The reference returns
/// <c>{"session_id": ..., "analysis": &lt;string&gt;}</c> — prose from the
/// connector (<c>registry.analyze_session -&gt; runtime.analyze</c>), or
/// <c>"connector offline"</c> when there is no connector.</para>
/// </summary>
public sealed class SessionLifecycleParityTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    /// <summary>Start a real server on *cfg*, the way the live driver does.</summary>
    private static async Task<(UtermServer Server, HttpClient Http)> StartAsync(UtermServerConfig cfg)
    {
        var port = FreePort();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "lifecycle-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig()),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "lifecycle",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return (server, http);
    }

    /// <summary>The default configuration plus one session that opts out of auto-start.</summary>
    private static UtermServerConfig ConfigWithManualSession()
    {
        var cfg = UtermServerConfig.Default();
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "manual",
            DisplayName = "Manual",
            ConnectorType = "shell",
            AutoStart = false,
        });
        return cfg;
    }

    // -- auto_start is acted on, not merely stored -----------------------------

    [Fact]
    public async Task An_Auto_Start_Session_Is_Running_When_The_Server_Has_Started()
    {
        var (server, http) = await StartAsync(UtermServerConfig.Default());
        await using (server)
        using (http)
        {
            var session = await http.GetFromJsonAsync<JsonElement>("/api/sessions/provide-shell");

            Assert.Equal("running", session.GetProperty("lifecycle_state").GetString());
            Assert.True(session.GetProperty("connected").GetBoolean());
        }
    }

    [Fact]
    public async Task A_Session_That_Opts_Out_Of_Auto_Start_Is_Left_Stopped()
    {
        var (server, http) = await StartAsync(ConfigWithManualSession());
        await using (server)
        using (http)
        {
            var session = await http.GetFromJsonAsync<JsonElement>("/api/sessions/manual");

            Assert.Equal("stopped", session.GetProperty("lifecycle_state").GetString());
            Assert.False(session.GetProperty("connected").GetBoolean());
        }
    }

    // -- the lifecycle vocabulary is the reference's ---------------------------

    [Fact]
    public void A_Registered_Session_Starts_In_The_References_Initial_State()
    {
        var registry = new InMemorySessionRegistry([UtermServerConfig.DefaultShellSession()]);

        Assert.True(registry.TryGetStatus("provide-shell", out var status));
        Assert.Equal("stopped", status.LifecycleState);
    }

    [Fact]
    public async Task Disconnecting_Reports_The_References_Stopped_Not_Disconnected()
    {
        var (server, http) = await StartAsync(UtermServerConfig.Default());
        await using (server)
        using (http)
        {
            var response = await http.PostAsync("/api/sessions/provide-shell/disconnect", null);

            response.EnsureSuccessStatusCode();
            var body = await response.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("stopped", body.GetProperty("lifecycle_state").GetString());
        }
    }

    [Fact]
    public void Every_State_This_Port_Can_Report_Is_In_The_References_Vocabulary()
    {
        var registry = new InMemorySessionRegistry([UtermServerConfig.DefaultShellSession()]);
        var seen = new List<string?>();

        registry.TryGetStatus("provide-shell", out var initial);
        seen.Add(initial.LifecycleState);
        seen.Add(registry.StartSession("provide-shell")?.LifecycleState);
        seen.Add(registry.StopSession("provide-shell")?.LifecycleState);
        seen.Add(registry.RestartSession("provide-shell")?.LifecycleState);
        registry.MarkWorker("provide-shell", online: false, isHijacked: false, inputMode: "open");
        registry.TryGetStatus("provide-shell", out var offline);
        seen.Add(offline.LifecycleState);

        Assert.All(seen, state => Assert.Contains(state, SessionLifecycleState.All));
    }

    // -- /analyze answers the reference's question -----------------------------

    [Fact]
    public async Task Analyze_Answers_Prose_Not_A_Status_Object()
    {
        var (server, http) = await StartAsync(UtermServerConfig.Default());
        await using (server)
        using (http)
        {
            var response = await http.PostAsync("/api/sessions/provide-shell/analyze", null);

            response.EnsureSuccessStatusCode();
            var body = await response.Content.ReadFromJsonAsync<JsonElement>();
            Assert.Equal("provide-shell", body.GetProperty("session_id").GetString());
            var analysis = body.GetProperty("analysis");
            // A string, as `registry.analyze_session() -> str` promises. An
            // object here would be session status filed under the analysis key.
            Assert.Equal(JsonValueKind.String, analysis.ValueKind);
            Assert.Contains("analysis", analysis.GetString()!, StringComparison.Ordinal);
        }
    }

    [Fact]
    public async Task Analyze_Says_The_Connector_Is_Offline_When_None_Is_Running()
    {
        var (server, http) = await StartAsync(ConfigWithManualSession());
        await using (server)
        using (http)
        {
            var response = await http.PostAsync("/api/sessions/manual/analyze", null);

            response.EnsureSuccessStatusCode();
            var body = await response.Content.ReadFromJsonAsync<JsonElement>();
            // The reference's own wording for "there is nothing to analyze"
            // (server/runtime.py: `if self._connector is None`).
            Assert.Equal("connector offline", body.GetProperty("analysis").GetString());
        }
    }
}
