//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Configuration for <see cref="TermHub"/>.</summary>
public sealed class TermHubConfig
{
    public int DashboardHijackLeaseS { get; set; } = 45;
    public int MaxWsMessageBytes { get; set; } = 1_048_576;
    public int MaxInputChars { get; set; } = 10000;
    public int MaxBufferChars { get; set; } = 40000;
    public int MaxEventDataChars { get; set; } = 8192;
    public double BrowserRateLimitPerSec { get; set; } = 30;
    public double RestAcquireRateLimitPerSec { get; set; } = 5;
    public double RestSendRateLimitPerSec { get; set; } = 20;
    public string? WorkerToken { get; set; }
    public int EventDequeMaxlen { get; set; } = 2000;
    public int MaxWorkers { get; set; } = 10000;
    public Action<string, int>? OnMetric { get; set; }
    public Action<string, bool, string?>? OnHijackChanged { get; set; }

    /// <summary>
    /// Where the hub says which worker it refused, and why —
    /// <c>(level, message)</c>, with the reference's own levels
    /// (<c>"warning"</c>, <c>"info"</c>) and message text.
    ///
    /// The hub had no logging surface at all, so the decisions the reference
    /// logs had to be metric counters here instead. A counter is the actionable
    /// half — an operator can see that refusals are happening — but it cannot say
    /// <em>which</em> worker was refused, which is the one fact somebody
    /// debugging a session stuck in <c>hijack</c> needs. Both are now emitted:
    /// the counter answers "how often", the line answers "which one".
    ///
    /// A callback rather than a logging framework, injected exactly as
    /// <see cref="OnMetric"/> and <see cref="OnHijackChanged"/> are. The port
    /// carries no logging dependency and this is not the place to introduce one:
    /// a host that has a logger points this at it, and unset it is a no-op, so
    /// every existing embedder behaves as before.
    /// </summary>
    public Action<string, string>? OnLog { get; set; }

    public IClock? Clock { get; set; }
}

/// <summary>
/// Composes registry, limiter, approvals, lease, state, router, connection, presence.
/// Port of provide.uterm.server.bridge.hub TermHub.
/// </summary>
public sealed class TermHub : ILeaseHub
{
    public WorkerRegistry Registry { get; }
    public RateLimiter Limiter { get; }
    public InMemoryApprovalStore Approvals { get; }
    public HijackLeaseManager Lease { get; }
    public StateStore State { get; }
    public MessageRouter Router { get; }
    public ConnectionManager Conn { get; }
    public PresenceManager Presence { get; }
    /// <summary>Live event fanout for SSE/watch long-poll (Python/Go EventBus).</summary>
    public EventBus EventBus { get; }

    private readonly Action<string, string>? _onLog;

    internal object SharedLock { get; } = new();
    internal IClock Clock { get; }
    internal int MaxEventDataChars { get; }
    internal int MaxWorkers { get; }

    public string? WorkerToken { get; }

    public TermHub(TermHubConfig? config = null)
    {
        config ??= new TermHubConfig();
        Clock = ClockUtil.OrDefault(config.Clock);
        Registry = new WorkerRegistry();
        MaxEventDataChars = Math.Max(256, config.MaxEventDataChars <= 0 ? 8192 : config.MaxEventDataChars);
        MaxWorkers = Math.Max(1, config.MaxWorkers <= 0 ? 10000 : config.MaxWorkers);
        WorkerToken = config.WorkerToken;
        _onLog = config.OnLog;

        Limiter = new RateLimiter(
            config.RestAcquireRateLimitPerSec <= 0 ? 5 : config.RestAcquireRateLimitPerSec,
            config.RestSendRateLimitPerSec <= 0 ? 20 : config.RestSendRateLimitPerSec,
            Clock);

        Approvals = new InMemoryApprovalStore(Clock);
        State = new StateStore(
            Registry,
            SharedLock,
            Clock,
            Math.Max(100, config.MaxBufferChars <= 0 ? 40000 : config.MaxBufferChars),
            config.OnMetric,
            config.OnHijackChanged);

        Lease = new HijackLeaseManager(
            Registry,
            SharedLock,
            config.DashboardHijackLeaseS <= 0 ? 45 : config.DashboardHijackLeaseS,
            this,
            Clock);

        EventBus = new EventBus(onMetric: config.OnMetric);
        Router = new MessageRouter(this, config.EventDequeMaxlen <= 0 ? 2000 : config.EventDequeMaxlen);
        Conn = new ConnectionManager(this);
        Presence = new PresenceManager(this);
    }

    // -- ILeaseHub ------------------------------------------------------------

    public bool IsHijacked(WorkerTermState st) => State.IsHijacked(st);
    public bool IsDashboardHijackActive(WorkerTermState st) => State.IsDashboardHijackActive(st);
    public bool HasValidRestLease(WorkerTermState st) => State.HasValidRestLease(st);
    public bool CanSendInput(WorkerTermState st, object ws) => Presence.CanSendInput(st, ws);
    public void Metric(string name, int value) => State.Metric(name, value);

    /// <summary>
    /// Report a hub decision to the injected sink — see
    /// <see cref="TermHubConfig.OnLog"/>. A no-op when nothing is injected, so a
    /// call site may log unconditionally.
    ///
    /// Callers must invoke this <em>outside</em> <see cref="SharedLock"/>: the
    /// sink is host code, and running it under the hub's lock would let a host
    /// logger hold up every worker and browser on the server.
    /// </summary>
    public void Log(string level, string message) => _onLog?.Invoke(level, message);
    public void NotifyHijackChanged(string workerId, bool enabled, string? owner) =>
        State.NotifyHijackChanged(workerId, enabled, owner);

    public Task<(bool Ok, Exception? Error)> SendWorkerAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default) =>
        Conn.SendWorkerAsync(workerId, msg, ct);

    public Task BroadcastHijackStateAsync(string workerId, CancellationToken ct = default) =>
        Conn.BroadcastHijackStateAsync(workerId, ct);

    public Task AppendEventAsync(string workerId, string eventType, CancellationToken ct = default)
    {
        _ = ct;
        Router.AppendEvent(workerId, eventType, null);
        return Task.CompletedTask;
    }

    public Task PruneIfIdleAsync(string workerId, CancellationToken ct = default)
    {
        _ = ct;
        Router.PruneIfIdle(workerId);
        return Task.CompletedTask;
    }

    // -- Facade helpers used by the HTTP server --------------------------------

    public bool AllowRestAcquireFor(string clientId) => Conn.AllowRestAcquireFor(clientId);
    public bool AllowRestSendFor(string clientId) => Conn.AllowRestSendFor(clientId);

    public Task<(bool Ok, string Reason)> TryAcquireRestHijackAsync(
        string workerId, string owner, int leaseS, string hijackId, double monoNow, CancellationToken ct = default) =>
        Lease.TryAcquireRestAsync(workerId, owner, leaseS, hijackId, monoNow, ct);

    public double? ExtendHijackLease(string workerId, string hijackId, string owner, int leaseS, double monoNow) =>
        Lease.ExtendLease(workerId, hijackId, owner, leaseS, monoNow);

    public (bool Released, bool ShouldResume) ReleaseRestHijack(string workerId, string hijackId) =>
        Lease.ReleaseRest(workerId, hijackId);

    public HijackSession? GetRestSession(string workerId, string hijackId)
    {
        lock (SharedLock)
        {
            var st = Registry.Get(workerId);
            if (st?.HijackSession is null || st.HijackSession.HijackId != hijackId)
            {
                return null;
            }

            if (st.HijackSession.LeaseExpiresAt <= Clock.Monotonic())
            {
                return null;
            }

            return st.HijackSession;
        }
    }

    public Dictionary<string, object?> AppendEventData(string workerId, string eventType, Dictionary<string, object?>? data) =>
        Router.AppendEvent(workerId, eventType, data);

    public (bool BrowserExpired, bool RestExpired) CleanupExpiredHijack(string workerId) =>
        Lease.CleanupExpired(workerId);

    public int Shutdown() => State.Shutdown();
}
