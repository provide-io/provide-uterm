//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>
/// The three actions an <see cref="IInputPolicyGate"/> may return. Port of the
/// Go <c>hub.PolicyDecision.Action</c> strings, which are themselves the
/// reference's <c>ext.PolicyDecision.action</c> literals.
/// </summary>
public static class PolicyActions
{
    /// <summary>Forward the input to the worker.</summary>
    public const string Allow = "allow";

    /// <summary>Refuse the input outright and tell the browser.</summary>
    public const string Deny = "deny";

    /// <summary>Park the browser and register a pending approval request.</summary>
    public const string Hold = "hold";
}

/// <summary>
/// The context handed to a policy gate. Port of <c>ext.PolicyContext</c> /
/// Go <c>hub.PolicyContext</c>.
/// </summary>
public sealed class PolicyContext
{
    public required string WorkerId { get; init; }

    /// <summary>Authenticated subject id, or <c>"anonymous"</c>.</summary>
    public string ClientId { get; init; } = "anonymous";

    /// <summary>The browser's role for this worker, when it is registered.</summary>
    public string? Role { get; init; }

    /// <summary>What the principal is attempting — <c>"input"</c> on this path.</summary>
    public string? Action { get; init; }

    public IReadOnlyDictionary<string, object?> Metadata { get; init; } =
        new Dictionary<string, object?>();
}

/// <summary>
/// A gate's verdict on one input. Port of <c>ext.PolicyDecision</c>.
/// </summary>
public sealed class PolicyDecision
{
    /// <summary>One of <see cref="PolicyActions"/>. Anything unrecognised is treated as a deny.</summary>
    public string Action { get; init; } = PolicyActions.Allow;

    /// <summary>Caller-chosen approval id; a new one is minted when this is empty.</summary>
    public string? RequestId { get; init; }

    /// <summary>Seconds a held command stays pending. Mirrors the Pydantic default of 60.</summary>
    public int TimeoutS { get; init; } = 60;

    /// <summary>Free-text explanation, surfaced in the rejection banner.</summary>
    public string? Reason { get; init; }

    /// <summary>The default allow decision.</summary>
    public static PolicyDecision Allow() => new();
}

/// <summary>
/// The input-interception policy surface. Port of <c>ext.PolicyGate</c>.
/// </summary>
/// <remarks>
/// Async where Go is synchronous: a real gate calls out to a governance
/// service, and every other outbound call in this port is a
/// <see cref="Task"/>. A gate that decides locally returns a completed task.
/// </remarks>
public interface IInputPolicyGate
{
    Task<PolicyDecision> InterceptInputAsync(
        string data,
        PolicyContext context,
        CancellationToken cancellationToken = default);
}

/// <summary>
/// Allows everything. Port of <c>ext.NoOpPolicyGate</c>, and the hub default:
/// a deployment that configures no gate keeps the ungated input path, which
/// forwards keystrokes to the worker without building a policy context.
/// </summary>
public sealed class NoOpPolicyGate : IInputPolicyGate
{
    public Task<PolicyDecision> InterceptInputAsync(
        string data,
        PolicyContext context,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(PolicyDecision.Allow());
}
