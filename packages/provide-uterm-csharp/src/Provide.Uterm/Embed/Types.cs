//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Embed;

/// <summary>Lifecycle phases for an embedded multi-client proxy session.</summary>
public enum SessionLifecycle
{
    Created,
    Connecting,
    Negotiated,
    Connected,
    UpstreamLost,
    Reconnecting,
    ClientAttached,
    Shutdown,
}

/// <summary>Outcome of a byte-pipeline interceptor.</summary>
public enum InterceptAction
{
    /// <summary>Forward the (possibly replaced) payload.</summary>
    Pass,

    /// <summary>Forward <see cref="InterceptResult.Payload"/> instead of the original.</summary>
    Replace,

    /// <summary>Drop the payload; do not forward.</summary>
    Consume,

    /// <summary>Hold until <see cref="IUtermSession.FlushDeferredAsync"/>.</summary>
    Defer,

    /// <summary>
    /// Drop the original and inject <see cref="InterceptResult.Payload"/> as a new
    /// ordered unit on the same direction (re-enters the pipeline).
    /// </summary>
    Inject,
}

/// <summary>Slow-client queue policy. Upstream is never blocked by spectators.</summary>
public enum BackpressurePolicy
{
    /// <summary>Drop oldest queued chunk when full (default for spectators).</summary>
    DropOldest,

    /// <summary>Drop the newest chunk when full.</summary>
    DropNewest,

    /// <summary>Disconnect the client when its queue is full.</summary>
    Disconnect,
}

/// <summary>Direction of a byte unit in the ordered pipeline.</summary>
public enum ByteDirection
{
    UpstreamToApp,
    ClientToUpstream,
}

/// <summary>Wire-level diagnostic event (IAC / negotiation), not application payload.</summary>
public enum WireEventKind
{
    Iac,
    Negotiation,
    Diagnostic,
}

/// <summary>Result returned by <see cref="IByteInterceptor"/>.</summary>
public sealed class InterceptResult
{
    public InterceptAction Action { get; init; } = InterceptAction.Pass;

    /// <summary>Used by Replace and Inject.</summary>
    public byte[]? Payload { get; init; }

    public static InterceptResult Pass() => new() { Action = InterceptAction.Pass };

    public static InterceptResult Replace(byte[] payload) =>
        new() { Action = InterceptAction.Replace, Payload = payload };

    public static InterceptResult Consume() => new() { Action = InterceptAction.Consume };

    public static InterceptResult Defer() => new() { Action = InterceptAction.Defer };

    public static InterceptResult Inject(byte[] payload) =>
        new() { Action = InterceptAction.Inject, Payload = payload };
}

/// <summary>Per-client tags and free-form metadata for selective fan-out.</summary>
public sealed class ClientMetadata
{
    public string ClientId { get; init; } = "";
    public HashSet<string> Tags { get; init; } = new(StringComparer.Ordinal);
    public Dictionary<string, string> Attributes { get; init; } = new(StringComparer.Ordinal);
    public BackpressurePolicy Backpressure { get; init; } = BackpressurePolicy.DropOldest;
    public int QueueCapacity { get; init; } = 64;
}

/// <summary>Filter for <see cref="IUtermSession.SendToClientsAsync"/>.</summary>
public sealed class ClientFilter
{
    public IReadOnlyCollection<string>? RequireAnyTag { get; init; }
    public IReadOnlyCollection<string>? ExcludeTags { get; init; }
    public Func<ClientMetadata, bool>? Predicate { get; init; }

    public static ClientFilter All { get; } = new();

    public bool Matches(ClientMetadata meta)
    {
        if (ExcludeTags is not null)
        {
            foreach (var t in ExcludeTags)
            {
                if (meta.Tags.Contains(t))
                {
                    return false;
                }
            }
        }

        if (RequireAnyTag is not null && RequireAnyTag.Count > 0)
        {
            var any = false;
            foreach (var t in RequireAnyTag)
            {
                if (meta.Tags.Contains(t))
                {
                    any = true;
                    break;
                }
            }

            if (!any)
            {
                return false;
            }
        }

        return Predicate is null || Predicate(meta);
    }
}

public sealed class ByteChunkEventArgs : EventArgs
{
    public required ByteDirection Direction { get; init; }
    public required byte[] Data { get; init; }
    public string? ClientId { get; init; }
}

public sealed class WireEventArgs : EventArgs
{
    public required WireEventKind Kind { get; init; }
    public required byte[] Data { get; init; }
    public string Detail { get; init; } = "";
}

public sealed class SessionLifecycleEventArgs : EventArgs
{
    public required SessionLifecycle Phase { get; init; }
    public string Detail { get; init; } = "";
}

/// <summary>Options for <see cref="IEmbedHub.CreateSessionAsync"/>.</summary>
public sealed class EmbedSessionOptions
{
    public string SessionId { get; init; } = "";
    public IByteInterceptor? Interceptor { get; init; }
    public ITelnetPolicy? TelnetPolicy { get; init; }
    public IReadOnlyDictionary<string, object?>? Services { get; init; }
    public bool TransparentEightBit { get; init; } = true;
}

public sealed class ClientAttachOptions
{
    public required ClientMetadata Metadata { get; init; }
}
