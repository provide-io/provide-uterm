//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Claims;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tests.Server;

namespace Provide.Uterm.Tests;

public sealed class SessionLifecycleSecurityScenarioTests
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
    };

    [Fact]
    public async Task InterpretsEveryApplicableCSharpScenario()
    {
        var root = FindRepositoryRoot();
        var contractPath = Environment.GetEnvironmentVariable("SESSION_LIFECYCLE_SCENARIO_CONTRACT")
            ?? Path.Combine(root, "spec", "session_lifecycle_security_scenarios.json");
        var contract = JsonNode.Parse(await File.ReadAllTextAsync(contractPath))!.AsObject();
        var applicable = contract["scenarios"]!.AsArray()
            .Select(node => node!.AsObject())
            .Where(scenario => Status(scenario) != "unserved")
            .ToList();
        var observations = new List<Observation>();
        foreach (var scenario in applicable) observations.Add(await ExecuteAsync(scenario));
        Assert.Equal(applicable.Select(Id).Order(), observations.Select(item => item.Id).Order());

        if (Environment.GetEnvironmentVariable("SESSION_LIFECYCLE_SCENARIO_OUTPUT") is { Length: > 0 } output)
        {
            await File.WriteAllTextAsync(output, JsonSerializer.Serialize(observations, JsonOptions) + "\n");
            return;
        }

        for (var index = 0; index < applicable.Count; index++)
        {
            var actual = JsonSerializer.SerializeToNode(observations[index], JsonOptions)!.AsObject();
            foreach (var expected in Expected(contract, applicable[index]))
            {
                Assert.True(
                    JsonNode.DeepEquals(actual[expected.Key], expected.Value),
                    $"{Id(applicable[index])}.{expected.Key}: {actual[expected.Key]} != {expected.Value}");
            }
        }
    }

    private static async Task<Observation> ExecuteAsync(JsonObject scenario)
    {
        var input = scenario["input"]!.AsObject();
        return Text(input, "operation") switch
        {
            "fragment_message" => await ExecuteFragmentationAsync(scenario, input),
            "browser_quota" => await ExecuteQuotaAsync(scenario),
            "governed_input" => await ExecuteUnsupportedGovernanceAsync(scenario),
            "resume_ownership" => await ExecuteResumeAsync(scenario, input),
            "non_owner_hijack_step" => await ExecuteNonOwnerStepAsync(scenario),
            "owner_handoff" => await ExecuteOwnerHandoffAsync(scenario, input),
            "approval_expiry" => await ExecuteApprovalExpiryAsync(scenario, input),
            var operation => throw new InvalidOperationException($"unsupported operation {operation}"),
        };
    }

    private static async Task<Observation> ExecuteFragmentationAsync(JsonObject scenario, JsonObject input)
    {
        var evidence = await WebSocketFragmentationIntegrationTests.RunContractScenarioAsync(
            Text(input, "transport"),
            Text(input, "payload"),
            Number(input, "fragment_count"),
            Number(input, "oversized_bytes"));
        var observation = Empty(scenario, "served");
        observation.Route = Text(input, "transport") + "_websocket";
        observation.StatusCode = 101;
        observation.FragmentCount = evidence.FragmentCount;
        observation.PreFinalActions = evidence.PreFinalActions;
        observation.PostFinalActions = evidence.PostFinalActions;
        observation.OversizedRefused = evidence.OversizedRefused;
        observation.DeliveredPayloads.AddRange(evidence.DeliveredPayloads);
        return observation;
    }

    private static async Task<Observation> ExecuteQuotaAsync(JsonObject scenario)
    {
        const string workerId = "lifecycle-quota";
        var cfg = BaseConfig(workerId, out var token);
        cfg.MaxConnectionsPerPrincipal = 1;
        var hookCalls = 0;
        var hub = new TermHub(new TermHubConfig { MaxConnectionsPerPrincipal = 1 });
        var deps = new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            BrowserSetupHook = () =>
            {
                if (Interlocked.Increment(ref hookCalls) == 1)
                    throw new InvalidOperationException("injected setup failure");
                return Task.CompletedTask;
            },
        };
        await using var server = new UtermServer(deps);
        server.Build([$"http://127.0.0.1:{cfg.Server.Port}"]);
        await server.StartAsync();
        var uri = new Uri($"ws://127.0.0.1:{cfg.Server.Port}/ws/browser/{workerId}/term");

        using (var failedSetup = await ConnectAsync(uri, token))
        {
            var closed = await ReadAsync(failedSetup);
            Assert.True(closed.IsClose);
        }
        await WaitUntilAsync(() => (hub.Registry.Get(workerId)?.Browsers.Count ?? 0) == 0);

        using var first = await ConnectAsync(uri, token);
        await DrainHandshakeAsync(first);
        using (var rejected = await ConnectAsync(uri, token))
        {
            var close = await ReadAsync(rejected);
            Assert.True(close.IsClose);
            Assert.Equal((WebSocketCloseStatus)1008, close.CloseStatus);
        }
        await SendPingAndAwaitPongAsync(first);
        first.Abort();
        await WaitUntilAsync(() => (hub.Registry.Get(workerId)?.Browsers.Count ?? 0) == 0);
        using var recovered = await ConnectAsync(uri, token);
        await DrainHandshakeAsync(recovered);

        var observation = Empty(scenario, "served");
        observation.Route = "browser_websocket";
        observation.StatusCode = 1008;
        observation.Error = "too_many_connections";
        observation.AcceptedConnections = 2;
        observation.RejectedConnections = 1;
        observation.QuotaRecovered = true;
        observation.SetupRollbackVerified = true;
        return observation;
    }

    private static async Task<Observation> ExecuteUnsupportedGovernanceAsync(JsonObject scenario)
    {
        const string workerId = "lifecycle-governance";
        var cfg = BaseConfig(workerId, out var token);
        cfg.Governance.PolicyWebhookUrl = "https://policy.example.test/input";
        var worker = new CaptureWorker();
        var hub = new TermHub();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
        });
        server.Build([$"http://127.0.0.1:{cfg.Server.Port}"]);
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
        using var created = await http.PostAsJsonAsync(
            "/api/fanout/groups", new { name = "governed", worker_ids = new[] { workerId } });
        created.EnsureSuccessStatusCode();
        var group = await created.Content.ReadFromJsonAsync<JsonElement>();
        var groupId = group.GetProperty("group_id").GetString()!;
        using var response = await http.PostAsJsonAsync(
            $"/api/fanout/groups/{groupId}/send", new { data = "must-not-deliver" });
        Assert.Equal(HttpStatusCode.NotImplemented, response.StatusCode);
        Assert.Empty(worker.Messages);

        var observation = Empty(scenario, "unsupported");
        observation.Route = "http";
        observation.StatusCode = 501;
        observation.Error = "unsupported_governance";
        observation.PolicyDecision = "unsupported";
        return observation;
    }

    private static async Task<Observation> ExecuteResumeAsync(JsonObject scenario, JsonObject input)
    {
        var evidence = Text(input, "case") == "current_owner"
            ? await ResumeLifecycleIntegrationTests.RunCurrentOwnerContractScenarioAsync()
            : await ResumeLifecycleIntegrationTests.RunCompetingOwnerContractScenarioAsync();
        var observation = Empty(scenario, "served");
        observation.Route = "browser_websocket";
        observation.StatusCode = 101;
        observation.ResumeSucceeded = evidence.ResumeSucceeded;
        observation.OwnershipRestored = evidence.OwnershipRestored;
        observation.ReplayRejected = evidence.ReplayRejected;
        observation.CompetingOwnerPreserved = evidence.CompetingOwnerPreserved;
        return observation;
    }

    private static async Task<Observation> ExecuteNonOwnerStepAsync(JsonObject scenario)
    {
        var refused = await ResumeLifecycleIntegrationTests.RunNonOwnerStepContractScenarioAsync();
        var observation = Empty(scenario, "served");
        observation.Route = "browser_websocket";
        observation.StatusCode = 101;
        observation.NonOwnerRefused = refused;
        return observation;
    }

    /// <summary>
    /// Owner A takes the dashboard hijack lease, releases it, and successor B
    /// takes it — after which only B's keystrokes reach the worker.
    ///
    /// Every step runs over the public surface of a really-started server: two
    /// separately-authenticated browser sockets on
    /// <c>/ws/browser/{worker_id}/term</c>, a real worker socket on
    /// <c>/ws/worker/{worker_id}/term</c> whose received frames are the delivery
    /// evidence, and the hub's own <c>hijack_state</c> fan-out as the ownership
    /// evidence. Nothing is asserted against hub internals, and the refusal is
    /// proved by a barrier rather than a sleep: the stale owner's input frame is
    /// followed by a <c>ping</c> on the same socket, and the browser loop
    /// handles one frame at a time, so the <c>pong</c> cannot arrive until the
    /// input ahead of it has already been decided.
    /// </summary>
    private static async Task<Observation> ExecuteOwnerHandoffAsync(JsonObject scenario, JsonObject input)
    {
        var workerId = Text(input, "worker_id");
        var payload = Text(input, "payload");
        var stalePayload = "stale-" + payload;
        var cfg = BaseConfig(workerId, out _);
        // Arbitrated input: without a lease nobody may type, which is what makes
        // the handoff observable at all.
        cfg.Sessions[0].InputMode = InputModes.Hijack;
        var hub = new TermHub(new TermHubConfig { WorkerToken = cfg.Auth.WorkerBearerToken });
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
        });
        server.Build([$"http://127.0.0.1:{cfg.Server.Port}"]);
        await server.StartAsync();

        var wsBase = $"ws://127.0.0.1:{cfg.Server.Port}";
        using var workerSocket = new ClientWebSocket();
        workerSocket.Options.SetRequestHeader("Authorization", "Bearer " + cfg.Auth.WorkerBearerToken);
        await workerSocket.ConnectAsync(new Uri($"{wsBase}/ws/worker/{workerId}/term"), CancellationToken.None);
        var feed = new WorkerFeed(workerSocket);
        await WaitUntilAsync(() => hub.Registry.Get(workerId)?.WorkerWs is not null);

        var browserUri = new Uri($"{wsBase}/ws/browser/{workerId}/term");
        using var outgoing = await ConnectAsync(browserUri, MintToken(cfg.Auth, Text(input, "outgoing_owner")));
        await DrainHandshakeAsync(outgoing);
        using var incoming = await ConnectAsync(browserUri, MintToken(cfg.Auth, Text(input, "incoming_owner")));
        await DrainHandshakeAsync(incoming);

        await SendControlAsync(outgoing, "hijack_request");
        await AwaitHijackStateAsync(outgoing, hijacked: true, owner: "me");
        await SendControlAsync(outgoing, "hijack_release");
        await AwaitHijackStateAsync(outgoing, hijacked: false, owner: null);

        await SendControlAsync(incoming, "hijack_request");
        await AwaitHijackStateAsync(incoming, hijacked: true, owner: "me");
        // The outgoing owner is told, on its own socket, that the lease is held
        // by somebody else: the handoff is complete on both halves of the wire.
        await AwaitHijackStateAsync(outgoing, hijacked: true, owner: "other");

        await SendInputAsync(outgoing, stalePayload);
        await SendPingAndAwaitPongAsync(outgoing);
        await SendInputAsync(incoming, payload);
        await WaitUntilAsync(() => feed.Snapshot().Contains(payload));
        await SendPingAndAwaitPongAsync(incoming);
        var delivered = feed.Snapshot();
        await feed.StopAsync();

        var observation = Empty(scenario, "served");
        observation.Route = "browser_websocket";
        observation.StatusCode = 101;
        observation.HandoffCompleted = true;
        observation.StaleOwnerRefused = !delivered.Contains(stalePayload);
        observation.SuccessorOwnerAccepted = delivered.Contains(payload);
        observation.DeliveredPayloads.AddRange(delivered);
        return observation;
    }

    /// <summary>
    /// A command held for approval whose deadline has passed, claimed late by an
    /// admin over <c>POST /api/approvals/{request_id}/approve</c>.
    ///
    /// The deadline is crossed on the hub's own clock rather than by sleeping,
    /// so "the deadline has passed" is a fact and not a race. Both observations
    /// are read back off the public surface — whether the server still lists the
    /// request as pending (<c>GET /api/approvals</c>), and what the approve
    /// route answers — and this adapter reports what the server actually does.
    ///
    /// As of this writing that is <em>not</em> what the contract expects, and the
    /// mismatch is the finding, not an adapter shortfall:
    /// <see cref="InMemoryApprovalStore.CleanupExpired"/> is the only code that
    /// can move a request out of PENDING on its deadline, and nothing in
    /// <c>src/</c> calls it — there is no approvals sweep (Go runs one every 30s
    /// from <c>server/sweeps.go</c>; Python from <c>app/factory_sweeps.py</c>)
    /// and, unlike Python's <c>claim_request</c>, C#'s <c>Claim</c> does not
    /// re-check <c>expires_at</c>. So a past-deadline request stays claimable and
    /// the late approve is granted 200. Calling <c>CleanupExpired()</c> from this
    /// adapter would manufacture the very transition the "served" cell claims the
    /// server performs, so it deliberately does not.
    /// </summary>
    private static async Task<Observation> ExecuteApprovalExpiryAsync(JsonObject scenario, JsonObject input)
    {
        const string requestId = "lifecycle-late-claim";
        var workerId = Text(input, "worker_id");
        var cfg = BaseConfig(workerId, out _);
        var clock = new ManualClock(1_000.0);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var worker = new CaptureWorker();
        Assert.True(hub.Conn.RegisterWorker(workerId, worker));
        // The C# port has no policy gate that parks a command, so the held
        // request is seeded through the store the approvals routes read. The
        // observed behaviour below is entirely the routes'.
        hub.Approvals.Add(new ApprovalRequest
        {
            Id = requestId,
            WorkerId = workerId,
            // Not the approving admin: the two-person control refuses a
            // self-approval with 403 before it ever reaches the pending check.
            SubmitterId = "approval-submitter",
            Command = Text(input, "payload"),
            CreatedAt = clock.Wall(),
            ExpiresAt = clock.Wall() + 1.0,
        });
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
        });
        server.Build([$"http://127.0.0.1:{cfg.Server.Port}"]);
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue(
            "Bearer", MintToken(cfg.Auth, Text(input, "principal")));

        clock.SetWall(clock.Wall() + 2.0);
        using var listed = await http.GetAsync("/api/approvals");
        listed.EnsureSuccessStatusCode();
        var pending = await listed.Content.ReadFromJsonAsync<JsonArray>();
        var stillPending = pending!.Any(item => Text(item!.AsObject(), "id") == requestId);

        using var response = await http.PostAsJsonAsync($"/api/approvals/{requestId}/approve", new { });
        var body = JsonNode.Parse(await response.Content.ReadAsStringAsync())!.AsObject();
        var status = (int)response.StatusCode;
        var error = status >= 400 ? NormalizeApprovalError(Text(body, "detail")) : null;

        var observation = Empty(scenario, "served");
        observation.Route = "http";
        observation.StatusCode = status;
        observation.Error = error;
        // Expiry is only real if the server itself stopped holding the request
        // pending once its deadline passed.
        observation.ApprovalExpired = !stillPending;
        observation.LateApprovalRefused = status == 400 && error == "approval_not_pending";
        observation.DeliveredPayloads.AddRange(worker.Messages);
        return observation;
    }

    /// <summary>Collapse the approvals refusal detail onto the contract's token.</summary>
    private static string NormalizeApprovalError(string detail) => detail switch
    {
        "Approval request is not pending" => "approval_not_pending",
        "Approval request not found" => "approval_not_found",
        _ => detail,
    };

    /// <summary>
    /// A second real credential for the same server. <see cref="DevIdp.Setup"/>
    /// rewrites the config to jwt mode with a symmetric secret, so a token
    /// signed with that secret authenticates as a different principal.
    /// </summary>
    private static string MintToken(AuthConfig auth, string subject)
    {
        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(auth.JwtPublicKeyPem!));
        var now = DateTimeOffset.UtcNow;
        return new JwtSecurityTokenHandler().WriteToken(new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: [new Claim("sub", subject), new Claim(auth.JwtRolesClaim, "admin")],
            notBefore: now.UtcDateTime,
            expires: now.AddHours(1).UtcDateTime,
            signingCredentials: new SigningCredentials(key, SecurityAlgorithms.HmacSha256)));
    }

    private static async Task SendControlAsync(ClientWebSocket socket, string type)
    {
        var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = type });
        await socket.SendAsync(
            Encoding.UTF8.GetBytes(frame), WebSocketMessageType.Text, true, CancellationToken.None);
    }

    private static async Task SendInputAsync(ClientWebSocket socket, string text) =>
        await socket.SendAsync(
            Encoding.UTF8.GetBytes(text), WebSocketMessageType.Text, true, CancellationToken.None);

    /// <summary>Read this socket's frames until the hub reports the named ownership.</summary>
    private static async Task AwaitHijackStateAsync(ClientWebSocket socket, bool hijacked, string? owner)
    {
        for (var index = 0; index < 12; index++)
        {
            var frame = DecodeControl(await ReadAsync(socket));
            if (frame is null || Field(frame, "type") != "hijack_state") continue;
            if (frame.TryGetValue("hijacked", out var flag) && flag is bool actual && actual == hijacked
                && Field(frame, "owner") == owner)
            {
                return;
            }
        }

        throw new Xunit.Sdk.XunitException($"hijack_state hijacked={hijacked} owner={owner ?? "null"} not observed");
    }

    private static Dictionary<string, object?>? DecodeControl(WebSocketMessage message) =>
        !ControlChannelCodec.IsControlFrame(message.Payload)
            ? null
            : new ControlFrameDecoder()
                .Feed(Encoding.UTF8.GetString(message.Payload))
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control)
                .FirstOrDefault();

    private static string? Field(IReadOnlyDictionary<string, object?> frame, string key) =>
        frame.TryGetValue(key, out var value) ? value?.ToString() : null;

    private static UtermServerConfig BaseConfig(string workerId, out string token)
    {
        var cfg = UtermServerConfig.Default();
        var port = FreePort();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions =
        [
            new SessionDefinition
            {
                SessionId = workerId,
                DisplayName = workerId,
                AutoStart = false,
                InputMode = InputModes.Open,
                Visibility = "public",
                Owner = "admin",
            },
        ];
        token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "lifecycle-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        return cfg;
    }

    private static async Task<ClientWebSocket> ConnectAsync(Uri uri, string token)
    {
        var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader("Authorization", "Bearer " + token);
        await socket.ConnectAsync(uri, CancellationToken.None);
        return socket;
    }

    private static async Task DrainHandshakeAsync(ClientWebSocket socket)
    {
        for (var index = 0; index < 3; index++) Assert.False((await ReadAsync(socket)).IsClose);
    }

    private static async Task SendPingAndAwaitPongAsync(ClientWebSocket socket)
    {
        var ping = ControlChannelCodec.EncodeControlFrame(
            new Dictionary<string, object?> { ["type"] = "ping" });
        await socket.SendAsync(Encoding.UTF8.GetBytes(ping), WebSocketMessageType.Text, true, CancellationToken.None);
        for (var index = 0; index < 5; index++)
        {
            var message = await ReadAsync(socket);
            if (Encoding.UTF8.GetString(message.Payload).Contains("\"type\":\"pong\"", StringComparison.Ordinal))
                return;
        }
        throw new Xunit.Sdk.XunitException("pong not observed");
    }

    private static async Task<WebSocketMessage> ReadAsync(ClientWebSocket socket)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        return await WebSocketMessageReader.ReadAsync(socket, 2_000_000, timeout.Token);
    }

    private static async Task WaitUntilAsync(Func<bool> predicate)
    {
        for (var index = 0; index < 200 && !predicate(); index++) await Task.Delay(10);
        Assert.True(predicate());
    }

    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static string FindRepositoryRoot()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            if (File.Exists(Path.Combine(directory.FullName, "spec", "session_lifecycle_security_scenarios.json")))
                return directory.FullName;
        }
        throw new DirectoryNotFoundException("repository root not found");
    }

    private static string Id(JsonObject scenario) => Text(scenario, "id");
    private static string Status(JsonObject scenario) => Text(scenario["backends"]!["csharp"]!.AsObject(), "status");
    private static string Text(JsonObject value, string key) => value[key]?.GetValue<string>() ?? "";
    private static int Number(JsonObject value, string key) => value[key]?.GetValue<int>() ?? 0;

    private static JsonObject Expected(JsonObject contract, JsonObject scenario)
    {
        var expected = new JsonObject();
        foreach (var item in contract["result_defaults"]!.AsObject()) expected[item.Key] = item.Value?.DeepClone();
        foreach (var item in scenario["expected"]!.AsObject()) expected[item.Key] = item.Value?.DeepClone();
        foreach (var item in scenario["backends"]!["csharp"]!["expected"]!.AsObject())
            expected[item.Key] = item.Value?.DeepClone();
        return expected;
    }

    private static Observation Empty(JsonObject scenario, string status) => new() { Id = Id(scenario), Status = status };

    /// <summary>
    /// Everything a real worker socket receives that is not a control frame —
    /// which is exactly the browser keystrokes the hub chose to deliver.
    /// </summary>
    private sealed class WorkerFeed
    {
        private readonly List<string> _inputs = [];
        private readonly object _gate = new();
        private readonly CancellationTokenSource _cancellation = new();
        private readonly Task _pump;

        public WorkerFeed(ClientWebSocket socket) => _pump = PumpAsync(socket);

        public string[] Snapshot()
        {
            lock (_gate) return [.. _inputs];
        }

        public async Task StopAsync()
        {
            await _cancellation.CancelAsync();
            await _pump;
            _cancellation.Dispose();
        }

        private async Task PumpAsync(ClientWebSocket socket)
        {
            try
            {
                while (socket.State == WebSocketState.Open)
                {
                    var message = await WebSocketMessageReader.ReadAsync(socket, 2_000_000, _cancellation.Token);
                    if (message.IsClose) return;
                    if (ControlChannelCodec.IsControlFrame(message.Payload)) continue;
                    lock (_gate) _inputs.Add(Encoding.UTF8.GetString(message.Payload));
                }
            }
            catch
            {
                // The scenario finished, or the socket went away during teardown.
                // Whatever was recorded before that is the delivery evidence, and
                // the pump must not surface a teardown fault as a test failure.
            }
        }
    }

    private sealed class CaptureWorker : IWorkerWs
    {
        public List<string> Messages { get; } = [];
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Messages.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class Observation
    {
        public required string Id { get; init; }
        public required string Status { get; init; }
        public string Route { get; set; } = "";
        public int StatusCode { get; set; }
        public string? Error { get; set; }
        public int FragmentCount { get; set; }
        public int PreFinalActions { get; set; }
        public int PostFinalActions { get; set; }
        public bool OversizedRefused { get; set; }
        public int AcceptedConnections { get; set; }
        public int RejectedConnections { get; set; }
        public bool QuotaRecovered { get; set; }
        public bool SetupRollbackVerified { get; set; }
        public string? PolicyDecision { get; set; }
        public bool SignedRequest { get; set; }
        public List<string> DeliveredPayloads { get; } = [];
        public bool ResumeSucceeded { get; set; }
        public bool OwnershipRestored { get; set; }
        public bool ReplayRejected { get; set; }
        public bool CompetingOwnerPreserved { get; set; }
        public bool NonOwnerRefused { get; set; }
        public bool HandoffCompleted { get; set; }
        public bool StaleOwnerRefused { get; set; }
        public bool SuccessorOwnerAccepted { get; set; }
        public bool ApprovalExpired { get; set; }
        public bool LateApprovalRefused { get; set; }
    }
}
