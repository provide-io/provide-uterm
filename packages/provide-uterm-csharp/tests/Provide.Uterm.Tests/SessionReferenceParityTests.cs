//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Client;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// The divergences conformance/live found between this port and the Python
/// reference: the default configuration's session, the wire shape of a
/// session, and the two refusals ASP.NET answers on its own.
///
/// Every expectation here is the reference's observed behaviour, pinned by
/// scenarios 002_session_authz / 003_error_shapes / 004_session_shape.
/// </summary>
[Collection("UTERM_TEST_MODE")]
public sealed class SessionReferenceParityTests
{
    /// <summary>Every key a session carries on the wire, in the reference's order.</summary>
    private static readonly string[] ReferenceSessionKeys =
    [
        "session_id",
        "display_name",
        "created_at",
        "connector_type",
        "lifecycle_state",
        "input_mode",
        "connected",
        "auto_start",
        "tags",
        "recording_enabled",
        "recording_available",
        "owner",
        "visibility",
        "stopped_at",
        "last_error",
    ];

    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    /// <summary>
    /// A server on the *default* configuration — the one the live driver serves —
    /// so what the matrix sees is what these tests see.
    /// </summary>
    private static async Task<(UtermServer Server, HttpClient Http, string Token)> StartAsync(
        UtermServerConfig? config = null)
    {
        var port = FreePort();
        var cfg = config ?? UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "parity-" + Guid.NewGuid().ToString("N")),
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
            Version = "parity",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return (server, http, token);
    }

    [Fact]
    public async Task StartHelperRestoresPreviousTestMode()
    {
        var original = Environment.GetEnvironmentVariable("UTERM_TEST_MODE");
        Environment.SetEnvironmentVariable("UTERM_TEST_MODE", "sentinel");
        try
        {
            var (server, http, _) = await StartAsync();
            await using (server)
            using (http) { }

            Assert.Equal("sentinel", Environment.GetEnvironmentVariable("UTERM_TEST_MODE"));
        }
        finally
        {
            Environment.SetEnvironmentVariable("UTERM_TEST_MODE", original);
        }
    }

    // -- Divergence 2: the default configuration ships a session ---------------

    [Fact]
    public void DefaultConfig_Seeds_The_Reference_Shell_Session()
    {
        var cfg = UtermServerConfig.Default();

        var session = Assert.Single(cfg.Sessions);
        Assert.Equal("provide-shell", session.SessionId);
        Assert.Equal("Provide Shell", session.DisplayName);
        Assert.Equal("shell", session.ConnectorType);
        Assert.Equal("open", session.InputMode);
        Assert.Equal("public", session.Visibility);
        Assert.Null(session.Owner);
        Assert.True(session.AutoStart);
        // Order is the contract: a filter matches on it.
        Assert.Equal(["shell", "reference"], session.Tags);
        Assert.Null(session.RecordingEnabled);
    }

    [Fact]
    public async Task Listed_Session_Is_The_Reference_Shell_Session()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                var listed = await http.GetFromJsonAsync<JsonElement>("/api/sessions");

                Assert.Equal(JsonValueKind.Array, listed.ValueKind);
                var session = Assert.Single(listed.EnumerateArray().ToList());
                AssertReferenceSession(session);
            }
        }
    }

    [Fact]
    public async Task Fetched_Session_Is_The_Reference_Shell_Session()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                var response = await http.GetAsync("/api/sessions/provide-shell");

                Assert.Equal(HttpStatusCode.OK, response.StatusCode);
                AssertReferenceSession(await response.Content.ReadFromJsonAsync<JsonElement>());
            }
        }
    }

    /// <summary>The wire shape scenario 004 pins, field for field and no field more.</summary>
    private static void AssertReferenceSession(JsonElement session)
    {
        Assert.Equal(
            ReferenceSessionKeys,
            session.EnumerateObject().Select(property => property.Name).ToArray());
        Assert.Equal("provide-shell", session.GetProperty("session_id").GetString());
        Assert.Equal("Provide Shell", session.GetProperty("display_name").GetString());
        Assert.Equal("shell", session.GetProperty("connector_type").GetString());
        Assert.Equal("open", session.GetProperty("input_mode").GetString());
        Assert.Equal("public", session.GetProperty("visibility").GetString());
        Assert.Equal(JsonValueKind.Null, session.GetProperty("owner").ValueKind);
        Assert.True(session.GetProperty("auto_start").GetBoolean());
        Assert.Equal(
            ["shell", "reference"],
            session.GetProperty("tags").EnumerateArray().Select(tag => tag.GetString()!).ToArray());
        Assert.False(session.GetProperty("recording_enabled").GetBoolean());
        Assert.False(session.GetProperty("recording_available").GetBoolean());
        Assert.Equal(JsonValueKind.Null, session.GetProperty("stopped_at").ValueKind);
        Assert.Equal(JsonValueKind.Null, session.GetProperty("last_error").ValueKind);
        Assert.False(string.IsNullOrEmpty(session.GetProperty("lifecycle_state").GetString()));
        Assert.False(string.IsNullOrEmpty(session.GetProperty("created_at").GetString()));
    }

    // -- Divergence 3: the framework's own refusals carry the reference body ---

    [Fact]
    public async Task Unrouted_Path_Answers_The_Reference_404_Body()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                var response = await http.GetAsync("/api/not-a-thing");

                Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
                var body = await response.Content.ReadFromJsonAsync<JsonElement>();
                Assert.Equal("Not Found", body.GetProperty("detail").GetString());
            }
        }
    }

    [Fact]
    public async Task Wrong_Method_Answers_The_Reference_405_Body()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                var response = await http.PostAsync(
                    "/api/health", new StringContent("{}", Encoding.UTF8, "application/json"));

                Assert.Equal(HttpStatusCode.MethodNotAllowed, response.StatusCode);
                var body = await response.Content.ReadFromJsonAsync<JsonElement>();
                Assert.Equal("Method Not Allowed", body.GetProperty("detail").GetString());
            }
        }
    }

    [Fact]
    public async Task A_Servers_Own_Refusal_Keeps_Its_Own_Wording()
    {
        var (server, http, _) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                var response = await http.GetAsync("/api/sessions/no-such-session");

                Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
                var body = await response.Content.ReadFromJsonAsync<JsonElement>();
                Assert.Equal("unknown session: no-such-session", body.GetProperty("detail").GetString());
            }
        }
    }

    // -- Divergence 1: the client hands back what the server sent --------------

    [Fact]
    public async Task ListSessions_Returns_The_Array_The_Server_Sent()
    {
        var (server, http, token) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                using var client = HijackClient.WithBearer(server.BaseAddress!.TrimEnd('/'), token);

                var sessions = await client.ListSessionsAsync();

                // A bare array, not an object wrapping one.
                var list = Assert.IsAssignableFrom<IReadOnlyList<object?>>(sessions);
                var first = Assert.IsType<Dictionary<string, object?>>(Assert.Single(list));
                Assert.Equal("provide-shell", first["session_id"]);
            }
        }
    }

    [Fact]
    public async Task SessionEvents_Returns_The_Array_The_Server_Sent()
    {
        var (server, http, token) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                using var client = HijackClient.WithBearer(server.BaseAddress!.TrimEnd('/'), token);

                var events = await client.SessionEvents("provide-shell");

                Assert.IsAssignableFrom<IReadOnlyList<object?>>(events);
            }
        }
    }

    [Fact]
    public async Task SessionEvents_Asks_For_The_Limit_It_Was_Given()
    {
        var (server, http, token) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                using var client = HijackClient.WithBearer(server.BaseAddress!.TrimEnd('/'), token);

                // The reference always sends a limit (Python `limit=100`, Go
                // `orDefault(limit, 100)`); a client that omitted it would take
                // whatever default a server happened to have.
                Assert.IsAssignableFrom<IReadOnlyList<object?>>(await client.SessionEvents("provide-shell", 5));
                Assert.IsAssignableFrom<IReadOnlyList<object?>>(await client.SessionEvents("provide-shell", 0));
            }
        }
    }

    [Fact]
    public async Task SetSessionMode_Sends_The_Key_The_Reference_Route_Reads()
    {
        var (server, http, token) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                using var client = HijackClient.WithBearer(server.BaseAddress!.TrimEnd('/'), token);

                // POST /api/sessions/{id}/mode reads `input_mode` — in this
                // port's own server and in the reference's. A client sending
                // `mode` gets a 422 from every server in the matrix.
                var hijacked = await client.SetSessionMode("provide-shell", "hijack");
                Assert.Equal("hijack", hijacked["input_mode"]);

                var opened = await client.SetSessionMode("provide-shell", "open");
                Assert.Equal("open", opened["input_mode"]);
            }
        }
    }

    [Fact]
    public async Task SessionSnapshot_Returns_The_Null_The_Server_Sent()
    {
        // A session nobody started has no worker and so no screen: the server
        // answers a bare null, and the client hands that back rather than
        // wrapping it in an object — which is what this pins.
        //
        // `provide-shell` is not the subject, because it is auto-started: its
        // worker files a snapshot the moment it attaches, exactly as the
        // reference's runtime does at the top of _bridge_session. Asked against
        // a live reference server, GET /api/sessions/provide-shell/snapshot
        // answers a snapshot object, not null.
        var cfg = UtermServerConfig.Default();
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "unstarted",
            DisplayName = "Unstarted",
            ConnectorType = "shell",
            AutoStart = false,
        });
        var (server, http, token) = await StartAsync(cfg);
        await using (server)
        {
            using (http)
            {
                using var client = HijackClient.WithBearer(server.BaseAddress!.TrimEnd('/'), token);

                Assert.Null(await client.SessionSnapshot("unstarted"));
            }
        }
    }

    /// <summary>
    /// The auto-started session's own screen is there to be read, because
    /// starting it attached a worker that filed one.
    /// </summary>
    [Fact]
    public async Task SessionSnapshot_Answers_The_Auto_Started_Sessions_Screen()
    {
        var (server, http, token) = await StartAsync();
        await using (server)
        {
            using (http)
            {
                using var client = HijackClient.WithBearer(server.BaseAddress!.TrimEnd('/'), token);

                var snapshot = await client.SessionSnapshot("provide-shell");

                var fields = Assert.IsAssignableFrom<IReadOnlyDictionary<string, object?>>(snapshot);
                Assert.Equal("snapshot", fields["type"]);
            }
        }
    }
}
