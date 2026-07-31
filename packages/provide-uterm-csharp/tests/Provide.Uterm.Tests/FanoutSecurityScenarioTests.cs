//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Provide.Uterm.Fanout;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Tests;

public sealed class FanoutSecurityScenarioTests
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
    };

    [Fact]
    public async Task Executes_Every_Applicable_CSharp_Scenario()
    {
        var root = FindRepositoryRoot();
        var contractPath = Environment.GetEnvironmentVariable("FANOUT_SECURITY_SCENARIO_CONTRACT")
            ?? Path.Combine(root, "spec", "fanout_security_scenarios.json");
        var contract = JsonNode.Parse(await File.ReadAllTextAsync(contractPath))!.AsObject();
        await RunRouteScenarios(root);

        var applicable = contract["scenarios"]!.AsArray().Select(node => node!.AsObject())
            .Where(scenario => Status(scenario) != "unserved").ToList();
        var observations = new List<Observation>();
        foreach (var scenario in applicable)
        {
            var observation = await ExecuteScenario(scenario);
            var actual = JsonSerializer.SerializeToNode(observation, JsonOptions)!.AsObject();
            foreach (var expected in Expected(scenario))
            {
                Assert.True(JsonNode.DeepEquals(actual[expected.Key], expected.Value),
                    $"{scenario["id"]}.{expected.Key}: {actual[expected.Key]} != {expected.Value}");
            }
            observations.Add(observation);
        }
        Assert.Equal(applicable.Select(s => s["id"]!.GetValue<string>()).Order(),
            observations.Select(o => o.Id).Order());

        if (Environment.GetEnvironmentVariable("FANOUT_SECURITY_SCENARIO_OUTPUT") is { Length: > 0 } outputPath)
            await File.WriteAllTextAsync(outputPath, JsonSerializer.Serialize(observations, JsonOptions) + "\n");
    }

    private static async Task<Observation> ExecuteScenario(JsonObject scenario)
    {
        switch (scenario["id"]!.GetValue<string>())
        {
            case "unauthenticated_refusal":
            {
                var (controller, hub) = Build(["w1"], ["w1"]);
                await Assert.ThrowsAsync<FanoutAuthorizationException>(() => controller.SendAsync("g", "id", null));
                Assert.Empty(hub.Delivered);
                return Empty(scenario, 401, "authentication_required");
            }
            case "viewer_public_session_refusal":
            {
                var (controller, hub) = Build(["w1"], ["w1"]);
                var viewer = new Principal
                {
                    SubjectId = "viewer", Roles = StringSet.Of("viewer"), Scopes = StringSet.Of("*"),
                };
                await Assert.ThrowsAsync<FanoutAuthorizationException>(() => controller.SendAsync("g", "id", viewer));
                Assert.Empty(hub.Delivered);
                return Empty(scenario, 403, "global_admin_required");
            }
            case "dormant_member_default_reject":
                return Empty(scenario, 400, "unknown_member");
            case "dormant_member_permissive_admission":
            {
                var (controller, _) = Build(["missing"], []);
                Assert.NotNull(controller.GetGroup("g", "admin"));
                return Empty(scenario, 200, null);
            }
            case "current_authorization_revocation":
            {
                var (controller, hub) = Build(["w1", "w2"], ["w1", "w2"], ["w2"]);
                return FromResult(scenario, await controller.SendAsync("g", "id", Admin()), hub);
            }
            case "group_grant_non_bypass":
            {
                var (controller, hub) = Build(["w1"], ["w1"], ["w1"]);
                controller.GrantAccess("g", "admin", "admin");
                return FromResult(scenario, await controller.SendAsync("g", "id", Admin()), hub);
            }
            case "partial_member_failure":
            {
                var (controller, hub) = Build(["w1", "w2"], ["w1"]);
                return FromResult(scenario, await controller.SendAsync("g", "id", Admin()), hub);
            }
            case "policy_deny":
            case "policy_hold_release":
            {
                var hub = new ScenarioHub(["w1"]);
                var controller = WithoutAuthorizer(hub);
                await Assert.ThrowsAsync<FanoutAuthorizationException>(() => controller.SendAsync("g", "id", Admin()));
                Assert.Empty(hub.Delivered);
                Assert.Empty(hub.Observers);
                return Empty(scenario, 501, "unsupported_fail_closed");
            }
            case "missing_controller_dependencies":
            {
                var hub = new ScenarioHub(["w1"]);
                var controller = WithoutAuthorizer(hub);
                await Assert.ThrowsAsync<FanoutAuthorizationException>(() => controller.SendAsync("g", "id", Admin()));
                Assert.Empty(hub.Delivered);
                return Empty(scenario, 403, "authorization_unavailable");
            }
            case "immediate_output_capture":
            {
                var (controller, hub) = Build(["w1"], ["w1"], output: "immediate");
                return FromResult(scenario, await controller.SendAsync("g", "id", Admin()), hub);
            }
            case "store_read_isolation":
            {
                var store = new InMemoryGroupStore();
                var stored = Group(["w1"]);
                stored.CreatedBy = "admin";
                store.Save(stored);
                Assert.True(store.TryGet("g", out var read));
                read.WorkerIds.Add("w2");
                Assert.True(store.TryGet("g", out var again));
                Assert.Equal(["w1"], again.WorkerIds);
                return Empty(scenario, 200, null);
            }
            case "store_atomic_update":
            {
                var store = new InMemoryGroupStore();
                var stored = Group(["w1"]);
                stored.CreatedBy = "admin";
                store.Save(stored);
                await Task.WhenAll(new[] { "alice", "bob" }.Select(grantee =>
                    Task.Run(() => Assert.True(store.GrantAccess("g", grantee, "admin")))));
                Assert.True(store.TryGet("g", out var group));
                Assert.Equal(["alice", "bob"], group.Grants.Order());
                return Empty(scenario, 200, null);
            }
            case "total_response_deadline":
            {
                var (controller, hub) = Build(["w1"], ["w1"], hangReads: true);
                var clock = Stopwatch.StartNew();
                var result = await controller.SendAsync("g", "tail -f", Admin(), 1000, 20)
                    .WaitAsync(TimeSpan.FromMilliseconds(500));
                Assert.True(clock.ElapsedMilliseconds < 400);
                return FromResult(scenario, result, hub);
            }
            default:
                throw new InvalidOperationException("unimplemented applicable C# scenario");
        }
    }

    private static Controller WithoutAuthorizer(ScenarioHub hub)
    {
        var controller = new Controller(hub, new ControllerConfig { IdGen = () => "send" });
        controller.CreateGroup(Group(["w1"]), "admin");
        return controller;
    }

    private static (Controller Controller, ScenarioHub Hub) Build(
        IEnumerable<string> members,
        IEnumerable<string> connected,
        IEnumerable<string>? denied = null,
        string output = "ok",
        bool hangReads = false)
    {
        var hub = new ScenarioHub(connected, output, hangReads);
        var controller = new Controller(hub, new ControllerConfig
        {
            IdGen = () => "send", Authorizer = new ScenarioAuthorizer(denied ?? []),
        });
        controller.CreateGroup(Group(members), "admin");
        return (controller, hub);
    }

    private static Group Group(IEnumerable<string> members) => new()
    {
        GroupId = "g", Name = "fleet", WorkerIds = members.ToList(), Mode = "parallel",
        QuiesceMs = 5, MaxResponseMs = 100, DivergenceThreshold = 0.8,
    };

    private static Principal Admin() => new()
    {
        SubjectId = "admin", Roles = StringSet.Of("admin"), Scopes = StringSet.Of("*"),
    };

    private static Observation Empty(JsonObject scenario, int code, string? error) => new()
    {
        Id = scenario["id"]!.GetValue<string>(), Status = Status(scenario), StatusCode = code, Error = error,
    };

    private static Observation FromResult(JsonObject scenario, Result result, ScenarioHub hub)
    {
        var observation = Empty(scenario, 200, null);
        observation.DeliveredWorkers.AddRange(hub.Delivered);
        observation.ObserverNotifications.AddRange(hub.Observers);
        observation.FailedMembers.AddRange(result.FailedSessions);
        foreach (var row in result.Results.Where(row => row.Ok && row.OutputDelta is not null))
            observation.Output[row.WorkerId] = row.OutputDelta!;
        return observation;
    }

    private static string Status(JsonObject scenario) =>
        scenario["backends"]!["csharp"]!["status"]!.GetValue<string>();

    private static JsonObject Expected(JsonObject scenario)
    {
        var expected = scenario["expected"]!.AsObject().DeepClone().AsObject();
        foreach (var pair in scenario["backends"]!["csharp"]!["expected"]!.AsObject())
            expected[pair.Key] = pair.Value?.DeepClone();
        return expected;
    }

    private static async Task RunRouteScenarios(string root)
    {
        var start = new ProcessStartInfo("dotnet")
        {
            WorkingDirectory = Path.Combine(root, "packages", "provide-uterm-csharp"),
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var arg in new[]
        {
            "test", "tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj", "--no-build", "--no-restore", "--filter",
            "FullyQualifiedName~ServerFanoutTests.All_Routes_Require_Authenticated_Global_Admin_Before_Parse_Or_Lookup|FullyQualifiedName~ServerIntegrationControlPlaneRestTests.Fanout_Unknown_Members_Are_Strict_By_Default_And_Configurable|FullyQualifiedName~ServerIntegrationControlPlaneRestTests.Fanout_Configured_Unsupported_Governance_Fails_Closed",
        }) start.ArgumentList.Add(arg);
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        Assert.True(process.ExitCode == 0, (await stdout) + (await stderr));
    }

    private static string FindRepositoryRoot()
    {
        foreach (var start in new[] { Directory.GetCurrentDirectory(), AppContext.BaseDirectory })
        {
            for (var directory = new DirectoryInfo(start); directory is not null; directory = directory.Parent)
                if (File.Exists(Path.Combine(directory.FullName, "spec", "fanout_security_scenarios.json")))
                    return directory.FullName;
        }
        throw new DirectoryNotFoundException("repository root not found");
    }

    private sealed class Observation
    {
        public required string Id { get; init; }
        public required string Status { get; init; }
        public int StatusCode { get; init; }
        public string? Error { get; init; }
        public bool ApprovalRequired { get; init; }
        public string? ApprovalId { get; init; }
        public List<string> DeliveredWorkers { get; } = [];
        public List<string> ObserverNotifications { get; } = [];
        public List<string> FailedMembers { get; } = [];
        public Dictionary<string, string> Output { get; } = [];
    }

    private sealed class ScenarioAuthorizer(IEnumerable<string> denied) : IFanoutAuthorizer
    {
        private readonly HashSet<string> _denied = denied.ToHashSet();
        public bool IsGlobalAdmin(Principal principal) =>
            principal.Roles.Has("admin") && principal.AdminSessionScope is null;
        public bool CanReadMember(Principal principal, string workerId) => !_denied.Contains(workerId);
    }

    private sealed class ScenarioHub(IEnumerable<string> connected, string output = "ok", bool hangReads = false)
        : IFanoutHub
    {
        private readonly HashSet<string> _connected = connected.ToHashSet();
        private readonly string _output = output;
        private readonly bool _hangReads = hangReads;
        private readonly ConcurrentDictionary<string, ScenarioSubscription> _subscriptions = new();
        public List<string> Delivered { get; } = [];
        public List<string> Observers { get; } = [];

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            if (!_connected.Contains(workerId)) return Task.FromResult(false);
            lock (Delivered) Delivered.Add(workerId);
            if (!_hangReads) _subscriptions[workerId].Enqueue(new FanoutOutputEvent("term", _output));
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            if (Equals(msg["type"], "fanout_input")) lock (Observers) Observers.Add(workerId);
            return Task.CompletedTask;
        }

        public IFanoutOutputSubscription SubscribeOutput(string workerId) =>
            _subscriptions[workerId] = new ScenarioSubscription();
    }

    private sealed class ScenarioSubscription : IFanoutOutputSubscription
    {
        private readonly Channel<FanoutOutputEvent?> _events = Channel.CreateUnbounded<FanoutOutputEvent?>();
        public void Enqueue(FanoutOutputEvent item) => _events.Writer.TryWrite(item);
        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct) =>
            await _events.Reader.ReadAsync(ct);
        public ValueTask DisposeAsync()
        {
            _events.Writer.TryComplete();
            return ValueTask.CompletedTask;
        }
    }
}
