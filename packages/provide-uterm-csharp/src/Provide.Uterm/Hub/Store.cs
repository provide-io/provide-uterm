//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

public enum OwnershipPublicationExpectation
{
    RestHeld,
    DashboardHeld,
    Released,
}

public sealed record OwnershipPublicationToken
{
    private OwnershipPublicationToken(
        string workerId,
        long ownershipVersion,
        OwnershipPublicationExpectation expectation,
        bool enabled,
        string? publishedOwner,
        string? restHijackId,
        string? restOwner,
        object? dashboardOwner)
    {
        WorkerId = workerId;
        OwnershipVersion = ownershipVersion;
        Expectation = expectation;
        Enabled = enabled;
        PublishedOwner = publishedOwner;
        RestHijackId = restHijackId;
        RestOwner = restOwner;
        DashboardOwner = dashboardOwner;
    }

    public string WorkerId { get; }
    public long OwnershipVersion { get; }
    public OwnershipPublicationExpectation Expectation { get; }
    public bool Enabled { get; }
    public string? PublishedOwner { get; }
    public string? RestHijackId { get; }
    public string? RestOwner { get; }
    public object? DashboardOwner { get; }

    public static OwnershipPublicationToken RestHeld(
        string workerId,
        long ownershipVersion,
        string hijackId,
        string owner) => new(
            workerId,
            ownershipVersion,
            OwnershipPublicationExpectation.RestHeld,
            true,
            owner,
            hijackId,
            owner,
            null);

    public static OwnershipPublicationToken DashboardHeld(
        string workerId,
        long ownershipVersion,
        object owner,
        string? publishedOwner = null) => new(
            workerId,
            ownershipVersion,
            OwnershipPublicationExpectation.DashboardHeld,
            true,
            publishedOwner,
            null,
            null,
            owner);

    public static OwnershipPublicationToken Released(
        string workerId,
        long ownershipVersion) => new(
            workerId,
            ownershipVersion,
            OwnershipPublicationExpectation.Released,
            false,
            null,
            null,
            null,
            null);
}

/// <summary>Input-buffer + lifecycle/policy helper service.</summary>
public sealed class StateStore
{
    private readonly WorkerRegistry _registry;
    private readonly object _lock;
    private readonly IClock _clock;
    private readonly int _maxBufferChars;
    private readonly Action<string, int>? _onMetric;
    private readonly Action<string, bool, string?>? _onHijackChanged;
    private readonly object _buffersGate = new();
    private readonly Dictionary<object, string> _inputBuffers = new();
    private readonly object _hijackNotificationGatesLock = new();
    private readonly Dictionary<string, object> _hijackNotificationGates =
        new(StringComparer.Ordinal);

    public StateStore(
        WorkerRegistry registry,
        object sharedLock,
        IClock? clock = null,
        int maxBufferChars = 40000,
        Action<string, int>? onMetric = null,
        Action<string, bool, string?>? onHijackChanged = null)
    {
        _registry = registry;
        _lock = sharedLock;
        _clock = ClockUtil.OrDefault(clock);
        _maxBufferChars = maxBufferChars;
        _onMetric = onMetric;
        _onHijackChanged = onHijackChanged;
    }

    public static int ClampLease(int leaseS)
    {
        if (leaseS < 1) return 1;
        if (leaseS > 14400) return 14400;
        return leaseS;
    }

    public bool HasValidRestLease(WorkerTermState st) =>
        st.HijackSession is not null && st.HijackSession.LeaseExpiresAt > _clock.Monotonic();

    public bool IsDashboardHijackActive(WorkerTermState st)
    {
        if (st.HijackOwner is null) return false;
        if (st.HijackOwnerExpiresAt is null) return true;
        return st.HijackOwnerExpiresAt.Value > _clock.Monotonic();
    }

    public bool IsHijacked(WorkerTermState st) =>
        IsDashboardHijackActive(st) || HasValidRestLease(st);

    public void Metric(string name, int value = 1) => _onMetric?.Invoke(name, value);

    public void NotifyHijackChanged(string workerId, bool enabled, string? owner)
    {
        OwnershipPublicationToken? token;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            token = st is null
                ? null
                : CurrentPublicationToken(workerId, st, enabled, owner);
        }
        if (token is not null) NotifyHijackChanged(token);
    }

    /// <summary>
    /// Publish a captured ownership transition only while its generation,
    /// ownership kind, and owner identity still match current state. The
    /// per-worker notification gate establishes one callback order for all
    /// acquisition, release, force-release, expiry, and disconnect publishers.
    /// </summary>
    public bool NotifyHijackChanged(OwnershipPublicationToken token)
    {
        lock (HijackNotificationGate(token.WorkerId))
        {
            lock (_lock)
            {
                var st = _registry.Get(token.WorkerId);
                if (st is null
                    || st.HijackOwnershipVersion != token.OwnershipVersion
                    || !MatchesExpectedOwnership(st, token))
                {
                    return false;
                }
            }

            _onHijackChanged?.Invoke(
                token.WorkerId, token.Enabled, token.PublishedOwner);
            return true;
        }
    }

    private OwnershipPublicationToken? CurrentPublicationToken(
        string workerId,
        WorkerTermState st,
        bool enabled,
        string? owner)
    {
        if (!enabled)
        {
            return OwnershipPublicationToken.Released(
                workerId, st.HijackOwnershipVersion);
        }
        if (HasValidRestLease(st) && st.HijackSession is { } rest)
        {
            return OwnershipPublicationToken.RestHeld(
                workerId, st.HijackOwnershipVersion, rest.HijackId, rest.Owner);
        }
        if (IsDashboardHijackActive(st) && st.HijackOwner is { } dashboard)
        {
            return OwnershipPublicationToken.DashboardHeld(
                workerId, st.HijackOwnershipVersion, dashboard, owner);
        }
        return null;
    }

    private bool MatchesExpectedOwnership(
        WorkerTermState st,
        OwnershipPublicationToken token) => token.Expectation switch
    {
        OwnershipPublicationExpectation.RestHeld =>
            HasValidRestLease(st)
            && st.HijackSession is { } rest
            && rest.HijackId == token.RestHijackId
            && rest.Owner == token.RestOwner,
        OwnershipPublicationExpectation.DashboardHeld =>
            IsDashboardHijackActive(st)
            && ReferenceEquals(st.HijackOwner, token.DashboardOwner),
        OwnershipPublicationExpectation.Released =>
            !IsDashboardHijackActive(st) && !HasValidRestLease(st),
        _ => false,
    };

    private object HijackNotificationGate(string workerId)
    {
        lock (_hijackNotificationGatesLock)
        {
            if (!_hijackNotificationGates.TryGetValue(workerId, out var gate))
            {
                gate = new object();
                _hijackNotificationGates[workerId] = gate;
            }
            return gate;
        }
    }

    public void TouchActivity(string workerId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is not null)
            {
                st.LastActivityAt = _clock.Monotonic();
            }
        }
    }

    public WorkerTermState GetOrCreate(string workerId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null)
            {
                st = new WorkerTermState();
                _registry.Put(workerId, st);
            }

            return st;
        }
    }

    public (string Command, bool Ok) BufferAndGetCommand(object ws, string data)
    {
        lock (_buffersGate)
        {
            var buf = (_inputBuffers.TryGetValue(ws, out var existing) ? existing : "") + data;
            if (buf.Length > _maxBufferChars)
            {
                _inputBuffers.Remove(ws);
                return ("", false);
            }

            if (buf.Contains('\r') || buf.Contains('\n'))
            {
                _inputBuffers.Remove(ws);
                return (buf, true);
            }

            _inputBuffers[ws] = buf;
            return ("", false);
        }
    }

    public int Shutdown() => 0;
}
