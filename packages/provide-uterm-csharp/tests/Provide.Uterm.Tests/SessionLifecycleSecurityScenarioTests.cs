//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
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
    }
}
