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
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// What a worker socket may and may not do to the session it attaches to.
///
/// Attaching is not an act of authority. A worker connecting says "I am here";
/// it does not say what the session's arbitration is, and it does not speak for
/// any socket but its own. The reference keeps both of those straight — the
/// session's mode comes from its definition and from what the worker announces
/// (<c>server/config_schema_session.py:40</c> defaults it to <c>open</c>), and
/// the disconnect path checks socket identity before it touches anything
/// (<c>bridge/routes/websockets_impl.py:241-247</c>, all cleanup inside
/// <c>if should_broadcast</c>).
/// </summary>
public sealed class SessionWorkerAttachParityTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, HttpClient Http, int Port)> StartAsync()
    {
        var cfg = UtermServerConfig.Default();
        var port = FreePort();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "worker-attach-" + Guid.NewGuid().ToString("N")),
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
            Version = "worker-attach",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return (server, http, port);
    }

    private static StringContent Json(string body) => new(body, Encoding.UTF8, "application/json");

    private static async Task CreateSession(HttpClient http, string id) =>
        (await http.PostAsync("/api/sessions", Json(
            $$"""{"session_id": "{{id}}", "display_name": "{{id}}", "connector_type": "shell"}""")))
            .EnsureSuccessStatusCode();

    /// <summary>
    /// Attach a worker socket and wait for the server to have registered it.
    ///
    /// ConnectAsync returns when the CLIENT sees the 101, but the handler sends
    /// that from AcceptWebSocketAsync and only afterwards calls
    /// RegisterWorkerAsync — which is what sets the WorkerWs that
    /// <c>connected</c> is read from (UtermServer.EnrichStatus). Returning on
    /// the 101 alone therefore hands back a socket the server has not finished
    /// accepting, and the next status read races the registration: an
    /// intermittent <c>connected == false</c>, seen on Windows CI. This is the
    /// attach-side counterpart of CloseWorker's wait — both make what the tests
    /// assert next a reading of the decision rather than a race against it.
    ///
    /// The poll cannot see a REPLACEMENT, because a session being replaced on
    /// is already connected via the socket being displaced. WaitDisplaced is
    /// what covers that case.
    /// </summary>
    private static async Task<ClientWebSocket> AttachWorker(HttpClient http, int port, string id)
    {
        var ws = new ClientWebSocket();
        await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/worker/{id}/term"), CancellationToken.None);

        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (!(await Status(http, id)).GetProperty("connected").GetBoolean())
        {
            if (DateTime.UtcNow > deadline)
            {
                ws.Dispose();
                Assert.Fail($"server never registered the worker socket for session {id}");
            }

            await Task.Delay(10);
        }

        return ws;
    }

    /// <summary>
    /// Wait for a worker socket to observe its own displacement.
    ///
    /// RegisterWorkerAsync notifies the predecessor when the lease must resume
    /// and then aborts it unconditionally in its finally block
    /// (Hub/Connection.cs), so either arriving proves the successor's
    /// registration has finished — the one thing AttachWorker's status poll
    /// cannot establish for a second attach to the same session.
    /// </summary>
    private static async Task WaitDisplaced(ClientWebSocket predecessor)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var drain = new byte[256];
        try
        {
            await predecessor.ReceiveAsync(drain, deadline.Token);
        }
        catch (WebSocketException)
        {
            // Aborted rather than notified — the same signal, arriving as a
            // broken stream.
        }
    }

    /// <summary>
    /// Close a worker socket and wait for the server to be done with it.
    ///
    /// The handler disposes the socket only after its disconnect path has run,
    /// so the client seeing the far end go away is that path having finished —
    /// which makes what the tests assert next a reading of the decision rather
    /// than a race against it.
    /// </summary>
    private static async Task CloseWorker(ClientWebSocket ws)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        try
        {
            await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "bye", deadline.Token);
            var drain = new byte[256];
            while (ws.State is WebSocketState.Open or WebSocketState.CloseSent)
            {
                var received = await ws.ReceiveAsync(drain, deadline.Token);
                if (received.MessageType == WebSocketMessageType.Close) break;
            }
        }
        catch (WebSocketException)
        {
            // The server dropped the connection rather than answering the
            // close — the same signal, arriving as an end of stream.
        }

        ws.Dispose();
    }

    private static async Task<JsonElement> Status(HttpClient http, string id)
    {
        var status = await http.GetAsync($"/api/sessions/{id}");
        status.EnsureSuccessStatusCode();
        return await status.Content.ReadFromJsonAsync<JsonElement>();
    }

    /// <summary>
    /// A worker attaching to a session the hub has no state for must not turn
    /// arbitration on. The session's mode is its own — a definition says
    /// <c>open</c> until something with the authority to change it says
    /// otherwise, and a socket connecting has no such authority. Letting the
    /// attach write <c>hijack</c> hands out an exclusivity nobody asked for
    /// and no operator authorized, and the acquire that then succeeds is the
    /// proof it was handed out.
    /// </summary>
    [Fact]
    public async Task A_Worker_Attaching_Does_Not_Turn_On_Arbitration()
    {
        var (server, http, port) = await StartAsync();
        await using (server)
        using (http)
        {
            await CreateSession(http, "idle-open");

            using var worker = await AttachWorker(http, port, "idle-open");

            var status = await Status(http, "idle-open");
            // Attaching is reported — the session has a worker now.
            Assert.True(status.GetProperty("connected").GetBoolean());
            // But the mode it was configured with is untouched.
            Assert.Equal("open", status.GetProperty("input_mode").GetString());

            // And the acquire is refused for the mode, which is the observable
            // difference between "a worker attached" and "a worker attached
            // and quietly took the session private".
            var acquire = await http.PostAsync(
                "/worker/idle-open/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            Assert.Equal(HttpStatusCode.Conflict, acquire.StatusCode);
            Assert.Equal(
                "Hijack not available in open input mode.",
                (await acquire.Content.ReadFromJsonAsync<JsonElement>()).GetProperty("error").GetString());
        }
    }

    /// <summary>
    /// When a second worker takes over a session id, the first socket is no
    /// longer anybody's worker — and its eventual close must not be mistaken
    /// for the session going away. The reference does the identity check for
    /// exactly this reason and does every part of the teardown inside it
    /// (<c>bridge/routes/websockets_impl.py:109-111, 241-247</c>): a stale
    /// socket's cleanup running against the live one takes down a session that
    /// is still serving, while a lease is still held on it.
    /// </summary>
    [Fact]
    public async Task Closing_A_Displaced_Worker_Leaves_The_Live_One_Serving()
    {
        var (server, http, port) = await StartAsync();
        await using (server)
        using (http)
        {
            await CreateSession(http, "twice");
            var displaced = await AttachWorker(http, port, "twice");
            using var live = await AttachWorker(http, port, "twice");
            await WaitDisplaced(displaced);
            (await http.PostAsync("/worker/twice/input_mode", Json("""{"input_mode": "hijack"}""")))
                .EnsureSuccessStatusCode();
            var acquire = await http.PostAsync(
                "/worker/twice/hijack/acquire", Json("""{"owner": "tester", "lease_s": 30}"""));
            acquire.EnsureSuccessStatusCode();
            var hijackId = (await acquire.Content.ReadFromJsonAsync<JsonElement>())
                .GetProperty("hijack_id").GetString();

            await CloseWorker(displaced);

            var status = await Status(http, "twice");
            Assert.Equal("running", status.GetProperty("lifecycle_state").GetString());
            Assert.True(status.GetProperty("connected").GetBoolean());

            // And the lease on the live session is untouched.
            var heartbeat = await http.PostAsync(
                $"/worker/twice/hijack/{hijackId}/heartbeat", Json("""{"lease_s": 30}"""));
            Assert.Equal(HttpStatusCode.OK, heartbeat.StatusCode);
        }
    }

    /// <summary>
    /// The other half of that: a worker that was never displaced still reports
    /// its own disconnect. The identity check must not silence the real one.
    /// </summary>
    [Fact]
    public async Task Closing_The_Live_Worker_Still_Reports_The_Disconnect()
    {
        var (server, http, port) = await StartAsync();
        await using (server)
        using (http)
        {
            await CreateSession(http, "solo");
            var worker = await AttachWorker(http, port, "solo");
            Assert.True((await Status(http, "solo")).GetProperty("connected").GetBoolean());

            await CloseWorker(worker);

            var status = await Status(http, "solo");
            Assert.False(status.GetProperty("connected").GetBoolean());
            Assert.Equal("stopped", status.GetProperty("lifecycle_state").GetString());
            // Disconnecting is not a mode change either.
            Assert.Equal("open", status.GetProperty("input_mode").GetString());
        }
    }
}
