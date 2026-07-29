//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using System.Text.Json.Nodes;

namespace Provide.Uterm.Conformance;

/// <summary>
/// A driver fault: the driver itself could not do what was asked (a scenario it
/// cannot read, an action it does not know). Distinct from anything a server
/// answered — an HTTP 500 is an observation, not one of these.
/// </summary>
public sealed class LiveDriverException : Exception
{
    public LiveDriverException(string message)
        : base(message)
    {
    }

    public LiveDriverException(string message, Exception inner)
        : base(message, inner)
    {
    }
}

/// <summary>One scenario step (<c>conformance/live/schema/scenario.schema.json</c>).</summary>
public sealed class LiveStep
{
    /// <summary>Send the token the server driver reported (the schema default).</summary>
    public const string AuthToken = "token";

    /// <summary>Send no <c>Authorization</c> header at all.</summary>
    public const string AuthNone = "none";

    /// <summary>Send a bearer token no server issued.</summary>
    public const string AuthBad = "bad";

    public string Id { get; init; } = "";
    public string Action { get; init; } = "";
    public string Auth { get; init; } = AuthToken;
    public string? Path { get; init; }
    public string? SessionId { get; init; }

    /// <summary>Request body for <c>http_post</c>; null when the step carries none.</summary>
    public JsonNode? Body { get; init; }

    /// <summary>The worker whose lease a hijack action acts on.</summary>
    public string? WorkerId { get; init; }

    /// <summary>The lease itself — normally a reference to the acquiring step.</summary>
    public string? HijackId { get; init; }

    /// <summary>Who is taking a lease. Null leaves the library's own default.</summary>
    public string? Owner { get; init; }

    /// <summary>How long a lease runs. Null leaves the library's own default.</summary>
    public int? LeaseS { get; init; }

    /// <summary>What <c>hijack_send</c> types.</summary>
    public string? Keys { get; init; }

    /// <summary><c>open</c> or <c>hijack</c>, in the reference's own vocabulary.</summary>
    public string? InputMode { get; init; }

    /// <summary>How many events <c>session_events</c> reads. Null leaves the default.</summary>
    public int? Limit { get; init; }

    /// <summary>
    /// How many times the step is performed. One unless the scenario said
    /// otherwise.
    ///
    /// Not an argument of the action: it changes how often the step is done,
    /// never what is sent — some behaviour (a rate limiter) is only observable
    /// by exhausting something.
    /// </summary>
    public int Repeat { get; init; } = 1;

    /// <summary>
    /// The step exactly as the scenario wrote it.
    ///
    /// Kept because a reference (<see cref="LiveReference"/>) is resolved against
    /// what earlier steps recorded, at the moment the request is built rather
    /// than when the file was read — so the written form has to survive until
    /// then, and the resolved form is parsed from a rewritten copy of it.
    /// </summary>
    public JsonObject Raw { get; init; } = new();

    /// <summary>
    /// The ids this step's observations are recorded under.
    ///
    /// A step done once keeps its own id; a repeated step numbers its
    /// repetitions from zero (<c>flood.0</c>, <c>flood.1</c>, …) and records
    /// nothing under the bare id. Every repetition is recorded, never just the
    /// last: a scenario repeats a step because it expects the answers to stop
    /// being the same, and which repetition changed is the measurement — only
    /// the final answer would turn "the thirty-first request was refused" into
    /// "a request was refused", which is a different claim about a budget.
    ///
    /// There is no <c>repeat</c> of one — the schema's floor is two, and the
    /// harness holds every scenario to it before a driver sees it — so anything
    /// under two is the same single observation a step with no <c>repeat</c>
    /// makes, rather than a second spelling that renumbers everything.
    /// </summary>
    public IReadOnlyList<string> ObservationIds() =>
        Repeat < 2 ? [Id] : [.. Enumerable.Range(0, Repeat).Select(index => $"{Id}.{index}")];
}

/// <summary>
/// A scenario as the harness writes it. Only the parts a driver acts on are
/// modelled: <c>expect</c> is deliberately absent, because a driver never
/// evaluates an expectation.
/// </summary>
public sealed class LiveScenario
{
    /// <summary>The auth mode a server driver starts in when the scenario names none.</summary>
    public const string DefaultServerAuth = "dev_token";

    public string Id { get; init; } = "";
    public string Title { get; init; } = "";
    public string Auth { get; init; } = DefaultServerAuth;
    public IReadOnlyList<string> Requires { get; init; } = Array.Empty<string>();
    public IReadOnlyList<LiveStep> Steps { get; init; } = Array.Empty<LiveStep>();

    /// <summary>Read a scenario file. <paramref name="path"/>'s stem names an unreadable scenario.</summary>
    public static LiveScenario Load(string path)
    {
        string text;
        try
        {
            text = File.ReadAllText(path);
        }
        catch (IOException ex)
        {
            throw new LiveDriverException($"cannot read scenario {path}: {ex.Message}", ex);
        }
        catch (UnauthorizedAccessException ex)
        {
            throw new LiveDriverException($"cannot read scenario {path}: {ex.Message}", ex);
        }

        return Parse(text, System.IO.Path.GetFileNameWithoutExtension(path));
    }

    /// <summary>
    /// Parse scenario JSON. <paramref name="fallbackId"/> names the scenario when
    /// the document carries no <c>id</c>, so a result can still be matched to it.
    /// </summary>
    public static LiveScenario Parse(string json, string? fallbackId = null)
    {
        JsonNode? root;
        try
        {
            root = JsonNode.Parse(json);
        }
        catch (JsonException ex)
        {
            throw new LiveDriverException("scenario is not JSON: " + ex.Message, ex);
        }

        if (root is not JsonObject obj)
        {
            throw new LiveDriverException("scenario must be a JSON object");
        }

        var steps = ParseSteps(obj["steps"]);
        return new LiveScenario
        {
            Id = Str(obj, "id") ?? fallbackId ?? "",
            Title = Str(obj, "title") ?? "",
            Auth = Str(obj, "auth") ?? DefaultServerAuth,
            Requires = Strings(obj["requires"]),
            Steps = steps,
        };
    }

    private static IReadOnlyList<LiveStep> ParseSteps(JsonNode? node)
    {
        if (node is not JsonArray array || array.Count == 0)
        {
            // A scenario with no steps would otherwise "complete" having observed
            // nothing, which is the one result that cannot be wrong.
            throw new LiveDriverException("scenario has no steps");
        }

        var steps = new List<LiveStep>(array.Count);
        foreach (var element in array)
        {
            steps.Add(ParseStep(element));
        }

        return steps;
    }

    /// <summary>
    /// One step, read out of the JSON object a scenario wrote it as.
    ///
    /// Internal rather than private because <see cref="LiveReference"/> parses a
    /// step a second time, from a copy with its references replaced: one parser
    /// for the written form and the resolved form both, so a field a scenario
    /// can write is a field a reference can fill.
    /// </summary>
    internal static LiveStep ParseStep(JsonNode? node)
    {
        if (node is not JsonObject obj)
        {
            throw new LiveDriverException("scenario step must be a JSON object");
        }

        var id = Str(obj, "id");
        if (string.IsNullOrEmpty(id))
        {
            throw new LiveDriverException("scenario step has no id");
        }

        var action = Str(obj, "action");
        if (string.IsNullOrEmpty(action))
        {
            throw new LiveDriverException($"scenario step {id} has no action");
        }

        return new LiveStep
        {
            Id = id,
            Action = action,
            Auth = Str(obj, "auth") ?? LiveStep.AuthToken,
            Path = Str(obj, "path"),
            SessionId = Str(obj, "session_id"),
            WorkerId = Str(obj, "worker_id"),
            HijackId = Str(obj, "hijack_id"),
            Owner = Str(obj, "owner"),
            LeaseS = Int(obj, "lease_s"),
            Keys = Str(obj, "keys"),
            InputMode = Str(obj, "input_mode"),
            Limit = Int(obj, "limit"),
            Repeat = Int(obj, "repeat") ?? 1,
            // Deep-cloned: the parsed document is discarded, and a node still
            // owned by it cannot be attached to the request we build.
            Body = obj["body"]?.DeepClone(),
            Raw = obj.DeepClone().AsObject(),
        };
    }

    /// <summary>
    /// A string field, or the JSON text of a value that is not one.
    ///
    /// A reference may resolve to a number — an id a server answered as digits
    /// is still that id — and rendering it is the only way a string field can
    /// carry it to the wire. Go's <c>asText</c> is the same rule.
    /// </summary>
    private static string? Str(JsonObject obj, string key) => obj[key] switch
    {
        null => null,
        JsonValue value when value.TryGetValue<string>(out var text) => text,
        var other => other.ToJsonString(),
    };

    /// <summary>An integer field, or null when the step names none.</summary>
    private static int? Int(JsonObject obj, string key) =>
        obj[key] is JsonValue value && value.TryGetValue<int>(out var number) ? number : null;

    private static IReadOnlyList<string> Strings(JsonNode? node)
    {
        if (node is not JsonArray array)
        {
            return Array.Empty<string>();
        }

        var values = new List<string>(array.Count);
        foreach (var element in array)
        {
            if (element is JsonValue value && value.TryGetValue<string>(out var text))
            {
                values.Add(text);
            }
        }

        return values;
    }
}
