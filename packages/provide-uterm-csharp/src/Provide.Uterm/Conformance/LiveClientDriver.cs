//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Provide.Uterm.Client;

namespace Provide.Uterm.Conformance;

/// <summary>
/// The `client` role of the live driver: runs a scenario's steps against a
/// server driver and records what it saw.
///
/// It states no verdict. Every expectation is the harness's to evaluate, so a
/// 401 and a 500 are observations here, not failures.
/// </summary>
public static class LiveClientDriver
{
    /// <summary>The `bad` auth token — deliberately not one any server minted.</summary>
    public const string BadToken = "live-harness-token-no-server-issued";

    /// <summary>The action vocabulary, shared by every language's driver.</summary>
    public static class Actions
    {
        public const string Health = "health";
        public const string ListSessions = "list_sessions";
        public const string GetSession = "get_session";
        public const string SessionSnapshot = "session_snapshot";
        public const string SessionEvents = "session_events";
        public const string SetInputMode = "set_input_mode";
        public const string HijackAcquire = "hijack_acquire";
        public const string HijackHeartbeat = "hijack_heartbeat";
        public const string HijackSend = "hijack_send";
        public const string HijackStep = "hijack_step";
        public const string HijackSnapshot = "hijack_snapshot";
        public const string HijackRelease = "hijack_release";
        public const string HttpGet = "http_get";
        public const string HttpPost = "http_post";
    }

    /// <summary>
    /// The actions that address a worker's lease.
    ///
    /// Named as a set so that an action nobody implements is still an unknown
    /// action: a dispatcher that asked for a <c>worker_id</c> first would report
    /// a missing argument for <c>teleport</c> rather than that it has never
    /// heard of it.
    /// </summary>
    private static readonly HashSet<string> LeaseActions = new(StringComparer.Ordinal)
    {
        Actions.HijackAcquire,
        Actions.HijackHeartbeat,
        Actions.HijackSend,
        Actions.HijackStep,
        Actions.HijackSnapshot,
        Actions.HijackRelease,
    };

    /// <summary>
    /// Run every step in order. Steps that a server refused still count as run;
    /// only the driver's own faults produce <see cref="LiveResult.StatusError"/>.
    /// </summary>
    public static async Task<LiveResult> RunAsync(
        LiveScenario scenario, string baseUrl, string token, CancellationToken ct = default)
    {
        var missing = scenario.Requires.Where(r => !LiveDriver.Capabilities.Contains(r)).ToList();
        if (missing.Count > 0)
        {
            return Result(scenario, LiveResult.StatusUnsupported, [], "missing capability: " + string.Join(", ", missing));
        }

        var steps = new List<LiveStepResult>(scenario.Steps.Count);
        // What each step recorded, for the steps that refer to each other's
        // answers. Resolution happens as each request is built, which is the
        // only moment the value exists.
        var seen = new Dictionary<string, JsonObject>(StringComparer.Ordinal);
        foreach (var written in scenario.Steps)
        {
            try
            {
                var step = LiveReference.Resolve(written, seen);
                var observed = await RunStepAsync(step, baseUrl, token, ct).ConfigureAwait(false);
                seen[observed.Id] = observed.Fields();
                steps.Add(observed);
            }
            catch (LiveDriverException ex)
            {
                // An action this driver does not know, or a reference nobody can
                // resolve: an error, never a silent skip. What ran already is
                // still reported, so a reader can see how far it got.
                return Result(scenario, LiveResult.StatusError, steps, ex.Message);
            }
        }

        return Result(scenario, LiveResult.StatusCompleted, steps, null);
    }

    private static LiveResult Result(
        LiveScenario scenario, string status, IReadOnlyList<LiveStepResult> steps, string? error) => new()
        {
            ScenarioId = scenario.Id,
            Role = LiveResult.RoleClient,
            Status = status,
            Capabilities = LiveDriver.Capabilities,
            Steps = steps,
            Error = error,
        };

    private static async Task<LiveStepResult> RunStepAsync(
        LiveStep step, string baseUrl, string token, CancellationToken ct)
    {
        var recorder = new LiveStatusRecordingHandler();
        using var http = new HttpClient(recorder);
        var headers = AuthHeaders(step.Auth, token);
        var trimmed = baseUrl.TrimEnd('/');

        if (step.Action is Actions.HttpGet or Actions.HttpPost)
        {
            return await RunRawAsync(step, trimmed, headers, http, recorder, ct).ConfigureAwait(false);
        }

        using var client = new HijackClient(trimmed, headers: headers, httpClient: http);
        object? value = null;
        string? error = null;
        var ok = false;
        try
        {
            // The library's own answer: it returns for a 2xx and refuses otherwise.
            value = await InvokeLibraryAsync(step, client, ct).ConfigureAwait(false);
            ok = true;
        }
        catch (ApiException ex)
        {
            // A refusal the library decoded. Which refusal it was is the
            // recorder's to say; the body it decoded is still the library's.
            value = ex.Body;
        }
        catch (LiveDriverException)
        {
            throw;
        }
        catch (Exception ex)
        {
            error = Describe(ex);
        }

        return Observe(step.Id, recorder, ok, error, () => JsonSerializer.SerializeToNode(value));
    }

    /// <summary>
    /// What the client library answered, whatever shape that is. A list
    /// endpoint answers a list and a snapshot may answer nothing at all, so
    /// this is not narrowed to a dictionary — narrowing it is exactly the bug
    /// scenario 002 caught in this port's client.
    /// </summary>
    private static async Task<object?> InvokeLibraryAsync(
        LiveStep step, HijackClient client, CancellationToken ct) => step.Action switch
        {
            Actions.Health => await client.HealthAsync(ct).ConfigureAwait(false),
            Actions.ListSessions => await client.ListSessionsAsync(ct).ConfigureAwait(false),
            Actions.GetSession => await client.GetSessionAsync(RequireSessionId(step), ct).ConfigureAwait(false),
            Actions.SessionSnapshot => await client.SessionSnapshot(RequireSessionId(step), ct).ConfigureAwait(false),
            // A limit nobody set is the library's own default, which is the
            // reference client's: a driver holding a second copy of that number
            // is a second place for it to drift.
            Actions.SessionEvents =>
                await client.SessionEvents(RequireSessionId(step), step.Limit ?? 0, ct).ConfigureAwait(false),
            // The *session* route, POST /api/sessions/{id}/mode — not the worker
            // route /worker/{id}/input_mode, which is a different thing under a
            // similar name and would leave the session's own mode untouched.
            Actions.SetInputMode =>
                await client.SetSessionMode(RequireSessionId(step), Require(step, step.InputMode, "input_mode"), ct)
                    .ConfigureAwait(false),
            _ when LeaseActions.Contains(step.Action) => await InvokeLeaseAsync(step, client, ct).ConfigureAwait(false),
            _ => throw new LiveDriverException($"unknown action: {step.Action}"),
        };

    /// <summary>
    /// The lease lifecycle. Every one of these needs a worker, and every one but
    /// the acquire needs the lease the acquire answered with — which is what the
    /// reference grammar exists to carry.
    ///
    /// An owner, a lease length and a set of keys nobody named are the library's
    /// own defaults: <c>operator</c>, ninety seconds, and nothing typed at all.
    /// A driver inventing keys would be typing at a terminal on its own account.
    /// </summary>
    private static async Task<object?> InvokeLeaseAsync(LiveStep step, HijackClient client, CancellationToken ct)
    {
        var worker = Require(step, step.WorkerId, "worker_id");
        if (step.Action == Actions.HijackAcquire)
        {
            return await client
                .AcquireAsync(worker, step.Owner ?? "", step.LeaseS ?? 0, ct)
                .ConfigureAwait(false);
        }

        var lease = Require(step, step.HijackId, "hijack_id");
        return step.Action switch
        {
            Actions.HijackHeartbeat =>
                await client.HeartbeatAsync(worker, lease, step.LeaseS ?? 0, ct).ConfigureAwait(false),
            Actions.HijackSend =>
                await client.SendAsync(worker, lease, step.Keys ?? "", ct: ct).ConfigureAwait(false),
            Actions.HijackStep => await client.StepAsync(worker, lease, ct: ct).ConfigureAwait(false),
            // A snapshot with no wait named takes the library's own, which is
            // the wait the reference driver's snapshot() takes.
            Actions.HijackSnapshot => await client.SnapshotAsync(worker, lease, ct: ct).ConfigureAwait(false),
            // `hijack_release`, and only it: LeaseActions already settled which
            // actions reach here, so a case naming it would be a branch no
            // scenario could take.
            _ => await client.ReleaseAsync(worker, lease, ct).ConfigureAwait(false),
        };
    }

    private static async Task<LiveStepResult> RunRawAsync(
        LiveStep step,
        string baseUrl,
        IReadOnlyDictionary<string, string> headers,
        HttpClient http,
        LiveStatusRecordingHandler recorder,
        CancellationToken ct)
    {
        string? error = null;
        try
        {
            if (string.IsNullOrEmpty(step.Path))
            {
                throw new InvalidOperationException($"{step.Action} requires path");
            }

            var method = step.Action == Actions.HttpPost ? HttpMethod.Post : HttpMethod.Get;
            using var request = new HttpRequestMessage(method, baseUrl + step.Path);
            request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            foreach (var (key, value) in headers)
            {
                request.Headers.TryAddWithoutValidation(key, value);
            }

            if (step.Body is not null)
            {
                request.Content = new StringContent(step.Body.ToJsonString(), Encoding.UTF8, "application/json");
            }

            using var response = await http.SendAsync(request, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            error = Describe(ex);
        }

        // No client library is involved in a raw call: the response is the whole
        // observation, and 2xx is the whole of `ok`.
        return Observe(step.Id, recorder, recorder.Successful, error, () => null);
    }

    /// <summary>
    /// Shape one step's record: the status is the one seen underneath the
    /// library, <c>ok</c> and <c>body</c> are the library's own answer — except
    /// that a body no parser accepts collapses to the protocol's one placeholder
    /// rather than to whatever each client does with unparseable bytes.
    /// </summary>
    private static LiveStepResult Observe(
        string id, LiveStatusRecordingHandler recorder, bool ok, string? error, Func<JsonNode?> libraryBody)
    {
        if (!recorder.HasResponse)
        {
            return new LiveStepResult
            {
                Id = id,
                Status = null,
                Ok = false,
                Body = null,
                Error = error ?? "no response",
            };
        }

        // A body nobody can parse is the same observation in every language;
        // the bytes are not.
        var body = TryParseWire(recorder.RawBody, out var wire)
            ? libraryBody() ?? wire
            : JsonValue.Create(LiveStepResult.NonJsonBody);

        return new LiveStepResult
        {
            Id = id,
            Status = recorder.StatusCode,
            Ok = ok,
            Body = body,
            // A response — of any status — is an observation, not a driver fault.
            Error = null,
        };
    }

    private static string RequireSessionId(LiveStep step) => Require(step, step.SessionId, "session_id");

    /// <summary>
    /// An argument the action cannot be performed without.
    ///
    /// Not a <see cref="LiveDriverException"/>: the driver is intact and the
    /// scenario is not, and the reference driver records that as the step's own
    /// error rather than ending the run — so the harness sees which step was
    /// written short.
    /// </summary>
    private static string Require(LiveStep step, string? value, string field) =>
        string.IsNullOrEmpty(value)
            ? throw new InvalidOperationException($"{step.Action} requires {field}")
            : value;

    /// <summary>Header set for a step's <c>auth</c> mode.</summary>
    public static IReadOnlyDictionary<string, string> AuthHeaders(string auth, string token) => auth switch
    {
        LiveStep.AuthNone => new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase),
        LiveStep.AuthBad => new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["Authorization"] = "Bearer " + BadToken,
        },
        LiveStep.AuthToken => Bearer(token),
        _ => throw new LiveDriverException($"unknown auth mode: {auth}"),
    };

    private static Dictionary<string, string> Bearer(string token) =>
        new(StringComparer.OrdinalIgnoreCase) { ["Authorization"] = "Bearer " + token };

    private static bool TryParseWire(string raw, out JsonNode? node)
    {
        node = null;
        if (string.IsNullOrWhiteSpace(raw))
        {
            return false;
        }

        try
        {
            node = JsonNode.Parse(raw);
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private static string Describe(Exception ex) => ex.GetType().Name + ": " + ex.Message;
}
