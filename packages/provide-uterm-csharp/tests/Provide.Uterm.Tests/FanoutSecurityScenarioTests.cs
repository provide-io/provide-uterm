//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Provide.Uterm.Fanout;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

public sealed class FanoutSecurityScenarioTests
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
    };

    /// <summary>
    /// The response budget for scenarios that do not ask for one.
    /// </summary>
    /// <remarks>
    /// Nineteen of the twenty scenarios test authorization semantics -- who may
    /// send, who is refused, who is notified. Only <c>total_response_deadline</c>
    /// tests the deadline itself, and it names its own 20ms. So this number is
    /// incidental to every scenario that inherits it, and it must be far enough
    /// out that the clock never decides one.
    ///
    /// 100ms was not, here least of all: this port quiesced for 25ms where the
    /// other three quiesce for 1, so a quarter of the budget was gone before any
    /// output could settle. (That 25 is now 1 as well -- it was hiding a race in
    /// OutputCollector rather than describing this port, and the collector no
    /// longer has it.) A member whose budget expires is reported in
    /// <c>failed_members</c> by design -- that is exactly what
    /// <c>total_response_deadline</c> asserts -- and under load this port
    /// reached that state on a member that was authorized and delivered to,
    /// turning <c>current_authorization_revocation</c> into
    /// <c>failed_members: [w1, w2]</c> against an expected <c>[w2]</c>.
    ///
    /// Reproduced exactly by shrinking this number: 40ms passes, 30ms and 20ms
    /// produce that failure verbatim, and 1ms fails earlier still with nothing
    /// delivered at all. That 40ms floor on an idle machine against a 100ms
    /// ceiling is the whole margin, and a loaded runner spends it.
    ///
    /// Costs nothing. A collect returns once output has been quiet for
    /// QuiesceMs, not when the budget runs out, so raising the ceiling does not
    /// slow a scenario that behaves. Only <c>continuous_output</c> runs to the
    /// deadline, and that scenario sets its own.
    /// </remarks>
    private const int DefaultMaxResponseMs = 5_000;

    [Fact]
    public async Task Interprets_Every_Applicable_CSharp_Scenario()
    {
        var root = FindRepositoryRoot();
        var contractPath = Environment.GetEnvironmentVariable("FANOUT_SECURITY_SCENARIO_CONTRACT")
            ?? Path.Combine(root, "spec", "fanout_security_scenarios.json");
        var contract = JsonNode.Parse(await File.ReadAllTextAsync(contractPath))!.AsObject();
        var applicable = contract["scenarios"]!.AsArray().Select(node => node!.AsObject())
            .Where(scenario => Status(scenario) != "unserved").ToList();
        var observations = new List<Observation>();
        foreach (var scenario in applicable) observations.Add(await ExecuteScenario(scenario));
        Assert.Equal(applicable.Select(Id).Order(), observations.Select(item => item.Id).Order());

        if (Environment.GetEnvironmentVariable("FANOUT_SECURITY_SCENARIO_OUTPUT") is not { Length: > 0 } outputPath)
        {
            for (var index = 0; index < applicable.Count; index++)
            {
                var actual = JsonSerializer.SerializeToNode(observations[index], JsonOptions)!.AsObject();
                foreach (var expected in Expected(applicable[index]))
                    Assert.True(JsonNode.DeepEquals(actual[expected.Key], expected.Value),
                        $"{Id(applicable[index])}.{expected.Key}: {actual[expected.Key]} != {expected.Value}");
            }
        }
        else
        {
            await File.WriteAllTextAsync(outputPath, JsonSerializer.Serialize(observations, JsonOptions) + "\n");
        }
    }

    private static async Task<Observation> ExecuteScenario(JsonObject scenario)
    {
        var input = scenario["input"]!.AsObject();
        var surface = Text(input, "surface");
        if (surface is "rest" or "rest_release") return await ExecuteRoute(scenario, input);
        if (surface == "store") return await ExecuteStore(scenario, input);
        if (surface != "controller") throw new InvalidOperationException($"unsupported surface {surface}");
        return await ExecuteController(scenario, input);
    }

    private static async Task<Observation> ExecuteController(JsonObject scenario, JsonObject input)
    {
        var (controller, hub) = Build(input);
        var command = Text(input, "command");
        Result result;
        try
        {
            result = await controller.SendAsync(
                Text(input["group"]!.AsObject(), "id"), command, PrincipalFor(input),
                maxResponseMs: Number(input, "max_response_ms"));
        }
        catch (FanoutAuthorizationException exception)
        {
            var error = exception.Message.Contains("principal", StringComparison.OrdinalIgnoreCase)
                ? "authentication_required"
                : exception.Message.Contains("admin", StringComparison.OrdinalIgnoreCase)
                    ? "global_admin_required"
                    : "authorization_unavailable";
            var code = error == "authentication_required" ? 401 : 403;
            Assert.Empty(hub.Delivered);
            return Empty(scenario, code, command, error);
        }
        return FromResult(scenario, result, hub);
    }

    private static async Task<Observation> ExecuteStore(JsonObject scenario, JsonObject input)
    {
        var groupInput = input["group"]!.AsObject();
        var store = new InMemoryGroupStore();
        var group = Group(input);
        store.Save(group);
        switch (Text(input, "operation"))
        {
            case "store_read_isolation":
                Assert.True(store.TryGet(group.GroupId, out var read));
                read.WorkerIds.Add(Text(input, "mutation_member"));
                Assert.True(store.TryGet(group.GroupId, out var again));
                Assert.DoesNotContain(Text(input, "mutation_member"), again.WorkerIds);
                break;
            case "store_atomic_update":
                var grants = Strings(input["concurrent_grants"]);
                await Task.WhenAll(grants.Select(grantee => Task.Run(() =>
                    Assert.True(store.GrantAccess(group.GroupId, grantee, Text(groupInput, "creator"))))));
                Assert.True(store.TryGet(group.GroupId, out var stored));
                Assert.Equal(grants.Order(), stored.Grants.Order());
                break;
            default:
                throw new InvalidOperationException($"unsupported store operation {Text(input, "operation")}");
        }
        return Empty(scenario, 200, Text(input, "command"), null);
    }

    private static async Task<Observation> ExecuteRoute(JsonObject scenario, JsonObject input)
    {
        var groupInput = input["group"]!.AsObject();
        var actor = input["actor"]!.AsObject();
        var isAdmin = Flag(actor, "authenticated") && Strings(actor["roles"]).Contains("admin");
        var (controller, fanoutHub) = BuildRoute(input, preseedGroup: !isAdmin);
        var config = UtermServerConfig.Default();
        config.FanoutAllowUnknownMembers = Flag(groupInput, "allow_unknown_members");
        if (Text(input["policy"]!.AsObject(), "action") != "allow")
            config.Governance.PolicyWebhookUrl = "https://policy.example.test/fanout";
        // The routes resolve every member through their own registry and ask
        // their own authorizer for read access, so the fixture has to answer at
        // both — otherwise a member is only ever unknown, and a registered one
        // is readable purely because the actor is an admin.
        var visibility = input["visibility"]!.AsObject();
        var registry = new InMemorySessionRegistry(
            RegisteredMembers(visibility).Select(id => new SessionDefinition { SessionId = id, AutoStart = false }));
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(), Auth = new ScenarioAuthenticator(),
            Authz = new AuthorizationService(new ScenarioAuthorizationProvider(ServerReadable(visibility))),
            Config = config, Registry = registry, Fanout = controller,
        });
        server.Build(["http://127.0.0.1:0"]);
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        if (Text(input, "operation") == "create")
        {
            using var response = await Send(http, HttpMethod.Post, "/api/fanout/groups",
                JsonSerializer.Serialize(new { name = "fixture-group", worker_ids = Strings(groupInput["members"]) }), actor);
            var body = await response.Content.ReadAsStringAsync();
            return Empty(scenario, (int)response.StatusCode, Text(input, "command"), CanonicalRouteError(response.StatusCode, body));
        }

        var groupId = Text(groupInput, "id");
        if (isAdmin)
        {
            var creator = new JsonObject
            {
                ["subject"] = Text(groupInput, "creator"), ["authenticated"] = true,
                ["roles"] = new JsonArray("admin"),
            };
            using var created = await Send(http, HttpMethod.Post, "/api/fanout/groups",
                JsonSerializer.Serialize(new { name = "fixture-group", worker_ids = Strings(groupInput["members"]) }), creator);
            Assert.Equal(HttpStatusCode.OK, created.StatusCode);
            var body = JsonNode.Parse(await created.Content.ReadAsStringAsync())!.AsObject();
            groupId = body["group_id"]!.GetValue<string>();
        }
        using var sent = await Send(http, HttpMethod.Post, $"/api/fanout/groups/{groupId}/send",
            JsonSerializer.Serialize(new { data = Text(input, "command") }), actor);
        var sentBody = await sent.Content.ReadAsStringAsync();
        var observation = Empty(
            scenario, (int)sent.StatusCode, Text(input, "command"), CanonicalRouteError(sent.StatusCode, sentBody));
        observation.DeliveredWorkers.AddRange(fanoutHub.Delivered);
        observation.ObserverNotifications.AddRange(fanoutHub.Observers);

        if (Text(input["policy"]!.AsObject(), "action") != "allow")
        {
            var stored = controller.GetGroup(groupId, Text(actor, "subject"));
            Assert.NotNull(stored);
            Assert.Equal(Strings(groupInput["members"]), stored.WorkerIds);
            Assert.All(stored.WorkerIds, member => Assert.Contains(member, fanoutHub.ConnectedMembers));
            Assert.Empty(fanoutHub.SendAttempts);
            Assert.Empty(fanoutHub.Observers);
        }

        return observation;
    }

    private static async Task<HttpResponseMessage> Send(
        HttpClient http, HttpMethod method, string path, string body, JsonObject actor)
    {
        using var request = new HttpRequestMessage(method, path)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
        if (Flag(actor, "authenticated"))
        {
            request.Headers.Add("X-Test-Subject", Text(actor, "subject"));
            request.Headers.Add("X-Test-Role", Strings(actor["roles"]).FirstOrDefault() ?? "viewer");
        }
        return await http.SendAsync(request);
    }

    private static string? CanonicalRouteError(HttpStatusCode status, string body)
    {
        if ((int)status < 400) return null;
        if (status == HttpStatusCode.Unauthorized) return "authentication_required";
        if (body.Contains("admin", StringComparison.OrdinalIgnoreCase)) return "global_admin_required";
        if (body.Contains("unknown fan-out", StringComparison.OrdinalIgnoreCase)) return "unknown_member";
        if (body.Contains("no read access", StringComparison.OrdinalIgnoreCase)) return "member_read_forbidden";
        if (body.Contains("authorization", StringComparison.OrdinalIgnoreCase)) return "authorization_unavailable";
        if (status == HttpStatusCode.NotImplemented) return "unsupported_fail_closed";
        return "request_failed";
    }

    /// <summary>
    /// The sessions the routes' registry knows about. A readable session has to
    /// exist to be readable, so the visible set is the floor; naming
    /// <c>registered_members</c> only adds sessions that exist without being
    /// readable, which is what separates an unknown member from a forbidden one.
    /// </summary>
    private static List<string> RegisteredMembers(JsonObject visibility)
    {
        var readable = Strings(visibility["readable_members"]);
        var registered = visibility["registered_members"] is JsonArray declared
            ? Strings(declared)
            : new List<string>(readable);
        foreach (var workerId in readable)
            if (!registered.Contains(workerId))
                registered.Add(workerId);
        return registered;
    }

    /// <summary>What the server's own authorizer answers read access from.</summary>
    private static HashSet<string> ServerReadable(JsonObject visibility)
    {
        var readable = Strings(visibility["readable_members"]).ToHashSet();
        readable.ExceptWith(Strings(visibility["revoke_before_send"]));
        return readable;
    }

    /// <summary>
    /// What the fan-out controller's own authorizer answers read access from. A
    /// fixture may hand the controller a wider view than the server has, to
    /// prove that admission still follows the server's answer.
    /// </summary>
    private static HashSet<string> ControllerReadable(JsonObject visibility)
    {
        var declared = visibility["controller_readable_members"] is JsonArray widened
            ? Strings(widened)
            : Strings(visibility["readable_members"]);
        var readable = declared.ToHashSet();
        readable.ExceptWith(Strings(visibility["revoke_before_send"]));
        return readable;
    }

    private static (Controller Controller, ScenarioHub Hub) Build(JsonObject input)
    {
        var readable = ControllerReadable(input["visibility"]!.AsObject());
        var workers = input["workers"]!.AsObject();
        var hub = new ScenarioHub(
            Strings(workers["accepted_members"]), StringMap(workers["immediate_output"]),
            Flag(workers, "continuous_output"));
        var config = new ControllerConfig { IdGen = () => "approval" };
        if (!Flag(input, "omit_authorizers")) config.Authorizer = new ScenarioAuthorizer(readable);
        var controller = new Controller(hub, config);
        var group = Group(input);
        controller.CreateGroup(group, group.CreatedBy);
        return (controller, hub);
    }

    private static (Controller Controller, ScenarioHub Hub) BuildRoute(JsonObject input, bool preseedGroup)
    {
        var workers = input["workers"]!.AsObject();
        var hub = new ScenarioHub(
            Strings(workers["accepted_members"]), StringMap(workers["immediate_output"]),
            Flag(workers, "continuous_output"));
        var config = new ControllerConfig { IdGen = () => "route-group" };
        // A fixture may leave the controller unwired, so the route has to refuse
        // rather than admit on whatever checks remain.
        if (!Flag(input, "omit_authorizers"))
            config.Authorizer = new ScenarioAuthorizer(ControllerReadable(input["visibility"]!.AsObject()));
        var controller = new Controller(hub, config);
        if (preseedGroup)
        {
            var group = Group(input);
            controller.CreateGroup(group, group.CreatedBy);
        }
        return (controller, hub);
    }

    private static Group Group(JsonObject input)
    {
        var group = input["group"]!.AsObject();
        return new Group
        {
            GroupId = Text(group, "id"), Name = "fixture-group", WorkerIds = Strings(group["members"]),
            CreatedBy = Text(group, "creator"), Grants = Strings(group["grants"]), Mode = "parallel",
            QuiesceMs = 1,
            MaxResponseMs = Number(input, "max_response_ms") is > 0 and var value ? value : DefaultMaxResponseMs,
            DivergenceThreshold = 0.8,
        };
    }

    private static Principal? PrincipalFor(JsonObject input)
    {
        var actor = input["actor"]!.AsObject();
        if (!Flag(actor, "authenticated")) return null;
        return new Principal
        {
            SubjectId = Text(actor, "subject"), Roles = StringSet.Of(Strings(actor["roles"]).ToArray()),
            Scopes = StringSet.Of("*"),
        };
    }

    private static Observation Empty(JsonObject scenario, int code, string command, string? error) => new()
    {
        Id = Id(scenario), Status = Status(scenario), StatusCode = code, Error = error, Command = command,
    };

    private static Observation FromResult(JsonObject scenario, Result result, ScenarioHub hub)
    {
        var observation = Empty(scenario, 200, result.Command, null);
        observation.DeliveredWorkers.AddRange(hub.Delivered);
        observation.ObserverNotifications.AddRange(hub.Observers);
        observation.FailedMembers.AddRange(result.FailedSessions);
        foreach (var row in result.Results.Where(row => row.Ok && row.OutputDelta is not null))
            observation.Output[row.WorkerId] = row.OutputDelta!;
        return observation;
    }

    private static string Id(JsonObject scenario) => Text(scenario, "id");
    private static string Status(JsonObject scenario) =>
        scenario["backends"]!["csharp"]!["status"]!.GetValue<string>();
    private static string Text(JsonObject value, string key) => value[key]?.GetValue<string>() ?? "";
    private static bool Flag(JsonObject value, string key) => value[key]?.GetValue<bool>() ?? false;
    private static int Number(JsonObject value, string key) => value[key]?.GetValue<int>() ?? 0;
    private static List<string> Strings(JsonNode? node) =>
        node is JsonArray array ? array.Select(item => item!.GetValue<string>()).ToList() : [];
    private static Dictionary<string, string> StringMap(JsonNode? node) =>
        node is JsonObject value ? value.ToDictionary(pair => pair.Key, pair => pair.Value!.GetValue<string>()) : [];

    private static JsonObject Expected(JsonObject scenario)
    {
        var expected = scenario["expected"]!.AsObject().DeepClone().AsObject();
        foreach (var pair in scenario["backends"]!["csharp"]!["expected"]!.AsObject())
            expected[pair.Key] = pair.Value?.DeepClone();
        return expected;
    }

    private static string FindRepositoryRoot()
    {
        foreach (var start in new[] { Directory.GetCurrentDirectory(), AppContext.BaseDirectory })
            for (var directory = new DirectoryInfo(start); directory is not null; directory = directory.Parent)
                if (File.Exists(Path.Combine(directory.FullName, "spec", "fanout_security_scenarios.json")))
                    return directory.FullName;
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
        public required string Command { get; init; }
        public List<string> DeliveredWorkers { get; } = [];
        public List<string> ObserverNotifications { get; } = [];
        public List<string> FailedMembers { get; } = [];
        public Dictionary<string, string> Output { get; } = [];
    }

    private sealed class ScenarioAuthorizer(IEnumerable<string> readable) : IFanoutAuthorizer
    {
        private readonly HashSet<string> _readable = readable.ToHashSet();
        public bool IsGlobalAdmin(Principal principal) =>
            principal.Roles.Has("admin") && principal.AdminSessionScope is null;
        public bool CanReadMember(Principal principal, string workerId) => _readable.Contains(workerId);
    }

    /// <summary>
    /// The server's authorizer, with the fixture answering read access. Every
    /// other decision still comes from local RBAC, so the admin and viewer
    /// refusals stay the server's own.
    /// </summary>
    private sealed class ScenarioAuthorizationProvider(IReadOnlySet<string> readable) : IAuthorizationProvider
    {
        private readonly LocalAuthorizationProvider _local = new();

        public StringSet CapabilitiesFor(Principal p) => _local.CapabilitiesFor(p);
        public bool HasCapability(Principal p, string capability) => _local.HasCapability(p, capability);
        public bool IsAdmin(Principal p) => _local.IsAdmin(p);
        public bool IsOwner(Principal p, SessionDefinition session) => _local.IsOwner(p, session);
        public bool CanReadSession(Principal p, SessionDefinition session) => readable.Contains(session.SessionId);

        public bool CanReadRecording(Principal p, SessionDefinition session) =>
            CanReadSession(p, session) && _local.HasCapability(p, "session.recording.read");

        public bool CanCreateSession(Principal p) => _local.CanCreateSession(p);

        public bool CanMutateSession(Principal p, SessionDefinition session, string action) =>
            _local.CanMutateSession(p, session, action);

        public string ResolveBrowserRole(Principal p, SessionDefinition session) =>
            CanReadSession(p, session) ? _local.ResolveBrowserRole(p, session) : "viewer";
    }

    private sealed class ScenarioHub(
        IEnumerable<string> accepted, IReadOnlyDictionary<string, string> output, bool continuousOutput) : IFanoutHub
    {
        private readonly HashSet<string> _accepted = accepted.ToHashSet();
        private readonly IReadOnlyDictionary<string, string> _output = output;
        private readonly bool _continuousOutput = continuousOutput;
        private readonly ConcurrentDictionary<string, ScenarioSubscription> _subscriptions = new();
        public IReadOnlySet<string> ConnectedMembers => _accepted;
        public List<string> SendAttempts { get; } = [];
        public List<string> Delivered { get; } = [];
        public List<string> Observers { get; } = [];

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> message, CancellationToken cancellationToken = default)
        {
            lock (SendAttempts) SendAttempts.Add(workerId);
            if (!_accepted.Contains(workerId)) return Task.FromResult(false);
            lock (Delivered) Delivered.Add(workerId);
            var subscription = _subscriptions[workerId];
            subscription.Enqueue(new FanoutOutputEvent("term", _output.GetValueOrDefault(workerId, "ok")));
            // The member the fixture says never stops. Previously the send
            // itself was left hanging instead, which reached the same
            // observation by never returning from dispatch -- so the collector,
            // the response budget and the truncation rule this scenario is
            // about were never run at all.
            if (_continuousOutput) subscription.NeverFallQuiet();
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> message, CancellationToken cancellationToken = default)
        {
            if (Equals(message["type"], "fanout_input")) lock (Observers) Observers.Add(workerId);
            return Task.CompletedTask;
        }

        public IFanoutOutputSubscription SubscribeOutput(string workerId) =>
            _subscriptions[workerId] = new ScenarioSubscription();
    }

    private sealed class ScenarioSubscription : IFanoutOutputSubscription
    {
        private readonly Channel<FanoutOutputEvent?> _events = Channel.CreateUnbounded<FanoutOutputEvent?>();
        private volatile bool _neverFallQuiet;
        public void Enqueue(FanoutOutputEvent item) => _events.Writer.TryWrite(item);

        /// <summary>Keep producing output for as long as anything reads it.</summary>
        public void NeverFallQuiet() => _neverFallQuiet = true;

        // Topped up on the way past rather than by a racing producer thread:
        // the fixture means "there is always more to come", and a background
        // writer would only sometimes manage to keep the queue non-empty.
        public int Pending => _neverFallQuiet ? Math.Max(1, _events.Reader.Count) : _events.Reader.Count;

        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken cancellationToken)
        {
            if (_neverFallQuiet) _events.Writer.TryWrite(new FanoutOutputEvent("term", "."));
            return await _events.Reader.ReadAsync(cancellationToken);
        }
        public ValueTask DisposeAsync()
        {
            _events.Writer.TryComplete();
            return ValueTask.CompletedTask;
        }
    }

    private sealed class ScenarioAuthenticator : IAuthenticator
    {
        public Task<Principal> AuthenticateAsync(AuthRequest request, CancellationToken cancellationToken = default)
        {
            var subject = request.Header("X-Test-Subject");
            if (subject.Length == 0) return Task.FromResult(Principal.Anonymous());
            return Task.FromResult(new Principal
            {
                SubjectId = subject, Roles = StringSet.Of(request.Header("X-Test-Role")), Scopes = StringSet.Of("*"),
            });
        }
    }
}
