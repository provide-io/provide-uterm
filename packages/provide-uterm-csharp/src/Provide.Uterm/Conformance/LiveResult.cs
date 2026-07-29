//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;

namespace Provide.Uterm.Conformance;

/// <summary>What the driver observed running one step.</summary>
public sealed class LiveStepResult
{
    /// <summary>The body observation for a response that was not JSON.</summary>
    public const string NonJsonBody = "<non-json>";

    public string Id { get; init; } = "";

    /// <summary>HTTP status, or null when no response was received at all.</summary>
    public int? Status { get; init; }

    /// <summary>True for a 2xx. Not a verdict — the harness judges.</summary>
    public bool Ok { get; init; }

    /// <summary>Parsed response body, or <see cref="NonJsonBody"/>, or null when there was none.</summary>
    public JsonNode? Body { get; init; }

    /// <summary>Set only when the step produced no response; a 500 is not an error.</summary>
    public string? Error { get; init; }

    public JsonObject ToJson() => new()
    {
        ["id"] = Id,
        ["fields"] = new JsonObject
        {
            ["status"] = Status is null ? null : JsonValue.Create(Status.Value),
            ["ok"] = Ok,
            ["body"] = Body?.DeepClone(),
            ["error"] = Error,
        },
    };
}

/// <summary>
/// What one driver observed running one scenario
/// (<c>conformance/live/schema/result.schema.json</c>).
///
/// There is no verdict here on purpose: <see cref="Status"/> describes the run,
/// not whether the server was right.
/// </summary>
public sealed class LiveResult
{
    /// <summary>Every step ran. The steps say what happened.</summary>
    public const string StatusCompleted = "completed";

    /// <summary>A required capability is missing.</summary>
    public const string StatusUnsupported = "unsupported";

    /// <summary>The driver itself failed.</summary>
    public const string StatusError = "error";

    public const string LanguageName = "csharp";
    public const string RoleClient = "client";
    public const string RoleServer = "server";

    public string ScenarioId { get; init; } = "";
    public string Role { get; init; } = RoleClient;
    public string Status { get; init; } = StatusCompleted;
    public IReadOnlyList<string> Capabilities { get; init; } = Array.Empty<string>();
    public IReadOnlyList<LiveStepResult> Steps { get; init; } = Array.Empty<LiveStepResult>();
    public string? Error { get; init; }

    public JsonObject ToJson()
    {
        var steps = new JsonArray();
        foreach (var step in Steps)
        {
            steps.Add(step.ToJson());
        }

        var capabilities = new JsonArray();
        foreach (var capability in Capabilities)
        {
            capabilities.Add(capability);
        }

        return new JsonObject
        {
            ["scenario_id"] = ScenarioId,
            ["language"] = LanguageName,
            ["role"] = Role,
            ["status"] = Status,
            ["capabilities"] = capabilities,
            ["steps"] = steps,
            ["error"] = Error,
        };
    }

    /// <summary>The single line of JSON a driver writes to stdout.</summary>
    public string ToJsonLine() => ToJson().ToJsonString();
}
