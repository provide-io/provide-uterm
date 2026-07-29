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

    private static LiveStep ParseStep(JsonNode? node)
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
            // Deep-cloned: the parsed document is discarded, and a node still
            // owned by it cannot be attached to the request we build.
            Body = obj["body"]?.DeepClone(),
        };
    }

    private static string? Str(JsonObject obj, string key) =>
        obj[key] is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;

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
