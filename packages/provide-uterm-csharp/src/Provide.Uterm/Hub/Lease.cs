//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Hub;

/// <summary>Callbacks the lease manager needs from the composing hub.</summary>
public interface ILeaseHub
{
    bool IsHijacked(WorkerTermState st);
    bool IsDashboardHijackActive(WorkerTermState st);
    bool HasValidRestLease(WorkerTermState st);
    bool CanSendInput(WorkerTermState st, object ws);
    void Metric(string name, int value);
    void NotifyHijackChanged(string workerId, bool enabled, string? owner);
    TimeSpan ResumeSendTimeout { get; }
    Task<(bool Ok, Exception? Error)> SendWorkerAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default);
    Task BroadcastHijackStateAsync(string workerId, CancellationToken ct = default);
    Task AppendEventAsync(string workerId, string eventType, CancellationToken ct = default);
    Task PruneIfIdleAsync(string workerId, CancellationToken ct = default);
}

/// <summary>Multi-worker hijack lease state machine.</summary>
public sealed class HijackLeaseManager
{
    private enum PauseDeliveryOutcome
    {
        NotDelivered,
        Delivered,
        PossiblyDelivered,
    }

    private sealed record PauseDeliveryResult(
        PauseDeliveryOutcome Outcome,
        Exception? Error = null);

    private readonly WorkerRegistry _registry;
    private readonly object _lock;
    private int _dashboardLeaseS;
    private readonly ILeaseHub _hub;
    private readonly IClock _clock;

    public HijackLeaseManager(
        WorkerRegistry registry,
        object sharedLock,
        int dashboardLeaseS,
        ILeaseHub hub,
        IClock? clock = null)
    {
        _registry = registry;
        _lock = sharedLock;
        _dashboardLeaseS = ClampDashboardLease(dashboardLeaseS);
        _hub = hub;
        _clock = ClockUtil.OrDefault(clock);
    }

    public int DashboardHijackLeaseS
    {
        get => _dashboardLeaseS;
        set => _dashboardLeaseS = ClampDashboardLease(value);
    }

    public static int ClampDashboardLease(int v)
    {
        if (v < 1) return 1;
        if (v > 600) return 600;
        return v;
    }

    public static Dictionary<string, object?> PauseFrame(string owner, string hijackId, double ts) => new()
    {
        ["type"] = "control",
        ["action"] = "pause",
        ["owner"] = owner,
        ["hijack_id"] = hijackId,
        ["ts"] = ts,
    };

    public static Dictionary<string, object?> ResumeFrame(string owner, double ts) => new()
    {
        ["type"] = "control",
        ["action"] = "resume",
        ["owner"] = owner,
        ["lease_s"] = 0,
        ["ts"] = ts,
    };

    /// <summary>
    /// Reserve REST hijack, pause worker outside the lock, then finalise.
    /// Returns (ok, reason). reason is empty on success.
    /// </summary>
    public async Task<(bool Ok, string Reason)> TryAcquireRestAsync(
        string workerId,
        string owner,
        int leaseS,
        string hijackId,
        double now,
        CancellationToken ct = default)
    {
        var reservation = "rest-pause-" + Guid.NewGuid().ToString("N");
        IWorkerWs? workerWs;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null || st.WorkerWs is null)
            {
                return (false, "no_worker");
            }

            if (st.InputMode == InputModes.Open)
            {
                return (false, "open_mode");
            }

            if (_hub.IsDashboardHijackActive(st)
                || _hub.HasValidRestLease(st)
                || st.HijackPending is not null
                || st.InputSendPending is not null)
            {
                return (false, "already_hijacked");
            }

            workerWs = st.WorkerWs;
            st.HijackPending = reservation;
            st.PendingPauseReservation = reservation;
        }

        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(PauseFrame(owner, hijackId, _clock.Wall()));
            var delivery = await SendPauseAsync(workerWs, encoded, ct).ConfigureAwait(false);
            if (delivery.Outcome != PauseDeliveryOutcome.Delivered)
            {
                await ResolvePauseObligationAsync(
                    workerId,
                    reservation,
                    workerWs,
                    pausePossiblyLanded: delivery.Outcome == PauseDeliveryOutcome.PossiblyDelivered)
                    .ConfigureAwait(false);
                if (delivery.Error is OperationCanceledException canceled)
                {
                    throw canceled;
                }
                if (delivery.Error is not null
                    || delivery.Outcome == PauseDeliveryOutcome.NotDelivered)
                {
                    lock (_lock)
                    {
                        var st = _registry.Get(workerId);
                        if (st is not null && ReferenceEquals(st.WorkerWs, workerWs))
                        {
                            st.WorkerWs = null;
                        }
                    }
                }

                return (false, "no_worker");
            }

            var committed = false;
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null
                    && st.HijackPending == reservation
                    && ReferenceEquals(st.WorkerWs, workerWs))
                {
                    st.HijackSession = new HijackSession
                    {
                        HijackId = hijackId,
                        Owner = owner,
                        AcquiredAt = now,
                        LeaseExpiresAt = now + leaseS,
                        LastHeartbeat = now,
                    };
                    st.HijackOwnershipVersion++;
                    if (st.PendingPauseObligation == reservation)
                    {
                        st.PendingPauseObligation = null;
                    }
                    ClearPauseReservation(st, reservation);
                    committed = true;
                }
            }

            if (!committed)
            {
                await ResolvePauseObligationAsync(
                    workerId, reservation, workerWs, pausePossiblyLanded: true)
                    .ConfigureAwait(false);
                return (false, "no_worker");
            }

            ArmExpiry(workerId, now + leaseS);
            return (true, "");
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null) ClearPauseReservation(st, reservation);
            }
        }
    }

    public (bool Ok, string Reason) TryAcquireWs(string workerId, object ws)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null || st.WorkerWs is null)
            {
                return (false, "no_worker");
            }

            if (!st.Browsers.ContainsKey(ws)
                || ws is IAbortableBrowserWs { IsActive: false })
            {
                return (false, "inactive_browser");
            }

            // HijackPending: REST two-phase reserve — treat as already taken so
            // the dashboard WS cannot dual-own during the pause I/O window.
            if (_hub.IsDashboardHijackActive(st)
                || _hub.HasValidRestLease(st)
                || st.HijackPending is not null
                || st.InputSendPending is not null)
            {
                return (false, "already_hijacked");
            }

            st.HijackOwner = ws;
            st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            st.HijackOwnershipVersion++;
            ArmExpiry(workerId, st.HijackOwnerExpiresAt.Value);
            return (true, "");
        }
    }

    /// <summary>
    /// Reserve a dashboard acquisition, pause the captured worker, then publish
    /// ownership only if the reservation and browser are still current.
    /// </summary>
    public async Task<(bool Ok, string Reason)> TryAcquireWsAsync(
        string workerId,
        object ws,
        long? ownershipVersion = null,
        CancellationToken ct = default)
    {
        var reservation = "dashboard-pause-" + Guid.NewGuid().ToString("N");
        IWorkerWs workerWs;

        while (true)
        {
            Task? disconnectResume = null;
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st?.WorkerWs is null) return (false, "no_worker");
                if (!st.Browsers.ContainsKey(ws)
                    || ws is IAbortableBrowserWs { IsActive: false })
                {
                    return (false, "inactive_browser");
                }

                if (ownershipVersion is { } expected
                    && st.HijackOwnershipVersion != expected)
                {
                    return (false, "ownership_changed");
                }

                if (st.DisconnectResumeCompletion is { IsCompleted: false } completion)
                {
                    if (ownershipVersion == st.DisconnectResumeOwnershipVersion)
                    {
                        disconnectResume = completion;
                    }
                    else
                    {
                        return (false, "already_hijacked");
                    }
                }
                else if (_hub.IsDashboardHijackActive(st)
                    || _hub.HasValidRestLease(st)
                    || st.HijackPending is not null
                    || st.InputSendPending is not null)
                {
                    return (false, "already_hijacked");
                }
                else
                {
                    workerWs = st.WorkerWs;
                    st.HijackPending = reservation;
                    st.PendingPauseReservation = reservation;
                    st.PendingDashboardBrowser = ws;
                    st.PendingDashboardOwnershipVersion = ownershipVersion;
                    break;
                }
            }

            await disconnectResume.WaitAsync(ct).ConfigureAwait(false);
        }

        var pauseLanded = false;
        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "control",
                ["action"] = "pause",
                ["source"] = "dashboard",
                ["ts"] = _clock.Wall(),
            });
            var delivery = await SendPauseAsync(workerWs, encoded, ct).ConfigureAwait(false);
            if (delivery.Outcome != PauseDeliveryOutcome.Delivered)
            {
                await ResolvePauseObligationAsync(
                    workerId,
                    reservation,
                    workerWs,
                    pausePossiblyLanded: delivery.Outcome == PauseDeliveryOutcome.PossiblyDelivered)
                    .ConfigureAwait(false);
                if (delivery.Error is OperationCanceledException canceled) throw canceled;
                return (false, "no_worker");
            }
            pauseLanded = true;

            lock (_lock)
            {
                var st = _registry.Get(workerId);
                var versionMatches = ownershipVersion is not { } expected
                    || st?.HijackOwnershipVersion == expected;
                if (st is not null
                    && ReferenceEquals(st.WorkerWs, workerWs)
                    && st.HijackPending == reservation
                    && ReferenceEquals(st.PendingDashboardBrowser, ws)
                    && st.PendingDashboardOwnershipVersion == ownershipVersion
                    && st.Browsers.ContainsKey(ws)
                    && ws is not IAbortableBrowserWs { IsActive: false }
                    && versionMatches
                    && !_hub.IsDashboardHijackActive(st)
                    && !_hub.HasValidRestLease(st))
                {
                    st.HijackOwner = ws;
                    st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
                    if (ownershipVersion is null) st.HijackOwnershipVersion++;
                    if (st.PendingPauseObligation == reservation)
                    {
                        st.PendingPauseObligation = null;
                    }
                    ClearPauseReservation(st, reservation);
                    ArmExpiry(workerId, st.HijackOwnerExpiresAt.Value);
                    return (true, "");
                }
            }

            await ResolvePauseObligationAsync(
                workerId, reservation, workerWs, pauseLanded)
                .ConfigureAwait(false);
            return (false, "inactive_browser");
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null) ClearPauseReservation(st, reservation);
            }
        }
    }

    private static void ClearPauseReservation(WorkerTermState st, string reservation)
    {
        if (st.HijackPending != reservation) return;
        st.HijackPending = null;
        if (st.PendingPauseReservation == reservation) st.PendingPauseReservation = null;
        st.PendingDashboardBrowser = null;
        st.PendingDashboardOwnershipVersion = null;
    }

    private static async Task<PauseDeliveryResult> SendPauseAsync(
        IWorkerWs worker,
        string encodedPause,
        CancellationToken ct)
    {
        if (worker is IAbortableBrowserWs { IsActive: false })
        {
            return new PauseDeliveryResult(PauseDeliveryOutcome.NotDelivered);
        }

        try
        {
            await worker.SendTextAsync(encodedPause, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException ex)
        {
            return new PauseDeliveryResult(PauseDeliveryOutcome.PossiblyDelivered, ex);
        }
        catch (Exception ex)
        {
            return new PauseDeliveryResult(PauseDeliveryOutcome.PossiblyDelivered, ex);
        }

        return worker is IAbortableBrowserWs { IsActive: false }
            ? new PauseDeliveryResult(PauseDeliveryOutcome.PossiblyDelivered)
            : new PauseDeliveryResult(PauseDeliveryOutcome.Delivered);
    }

    private async Task ResolvePauseObligationAsync(
        string workerId,
        string canceledReservation,
        IWorkerWs pausedWorker,
        bool pausePossiblyLanded)
    {
        var resumeReservation = "pause-obligation-resume-" + Guid.NewGuid().ToString("N");
        var reservedResume = false;
        var shouldResume = false;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null) return;
            ClearPauseReservation(st, canceledReservation);
            var resumeRequired = pausePossiblyLanded
                || st.PendingPauseObligation == canceledReservation;
            if (!resumeRequired) return;

            if (!ReferenceEquals(st.WorkerWs, pausedWorker))
            {
                if (st.PendingPauseObligation == canceledReservation)
                {
                    st.PendingPauseObligation = null;
                }
                // Registry ownership now describes a different worker. Repair
                // the captured old worker directly without mutating the new
                // worker's reservation/owner state.
                shouldResume = true;
            }
            else
            {
                if (_hub.IsDashboardHijackActive(st) || _hub.HasValidRestLease(st))
                {
                    st.PendingPauseObligation = null;
                    return;
                }

                if (st.DisconnectResumeCompletion is { IsCompleted: false })
                {
                    st.PendingPauseObligation = null;
                    return;
                }

                if (st.HijackPending is not null
                    && st.PendingPauseReservation == st.HijackPending)
                {
                    st.PendingPauseObligation = st.HijackPending;
                    return;
                }

                if (st.HijackPending is not null) return;

                st.PendingPauseObligation = null;
                st.HijackPending = resumeReservation;
                reservedResume = true;
                shouldResume = true;
            }
        }

        if (!shouldResume) return;

        try
        {
            var encodedResume = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "control",
                ["action"] = "resume",
                ["source"] = "dashboard",
                ["ts"] = _clock.Wall(),
            });
            await pausedWorker.SendTextAsync(encodedResume, CancellationToken.None).ConfigureAwait(false);
        }
        catch
        {
            // Best-effort state repair after an acquisition that cannot own.
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (reservedResume && st?.HijackPending == resumeReservation)
                {
                    st.HijackPending = null;
                }
            }
        }
    }

    /// <summary>Restore the same logical dashboard owner only if no later owner has existed.</summary>
    public bool TryRestoreWsOwnership(string workerId, object ws, long ownershipVersion)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null
                || st.WorkerWs is null
                || !st.Browsers.ContainsKey(ws)
                || st.HijackOwnershipVersion != ownershipVersion
                || _hub.IsDashboardHijackActive(st)
                || _hub.HasValidRestLease(st)
                || st.HijackPending is not null
                || st.InputSendPending is not null)
            {
                return false;
            }

            st.HijackOwner = ws;
            st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            ArmExpiry(workerId, st.HijackOwnerExpiresAt.Value);
            return true;
        }
    }

    public double? ExtendLease(string workerId, string hijackId, string owner, int leaseS, double now)
    {
        double? expiration;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st?.HijackSession is null || st.HijackSession.HijackId != hijackId)
            {
                return null;
            }

            if (st.HijackSession.Owner != owner)
            {
                _hub.Metric("hijack_heartbeat_denied_owner_mismatch", 1);
                return null;
            }

            st.HijackSession.LastHeartbeat = now;
            st.HijackSession.LeaseExpiresAt = now + leaseS;
            expiration = st.HijackSession.LeaseExpiresAt;
        }

        ArmExpiry(workerId, expiration.Value);
        return expiration;
    }

    public async Task<(bool Released, bool ShouldResume)> ReleaseRestAsync(
        string workerId,
        string hijackId,
        CancellationToken ct = default)
    {
        var reservation = "rest-release-resume-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? queuedTransition = null;
        IWorkerWs? worker = null;
        var shouldResume = false;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    if (st?.HijackSession is null || st.HijackSession.HijackId != hijackId)
                    {
                        return (false, false);
                    }
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        queuedTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= queuedTransition.Completion;
                        pendingCompletion = queuedTransition.Activated.Task;
                    }
                    else
                    {
                        completion ??= ReserveLifecycleTransition(st, reservation);
                        if (st.InputSendPending is not null)
                        {
                            pendingCompletion = st.InputSendPending.Completion.Task;
                        }
                        else
                        {
                            st.HijackSession = null;
                            shouldResume = !_hub.IsDashboardHijackActive(st);
                            worker = shouldResume ? st.WorkerWs : null;
                            break;
                        }
                    }
                }

                await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
            }
            transitionReady = true;
        }
        finally
        {
            if (!transitionReady && completion is not null)
            {
                CompleteTransitionReservation(workerId, reservation, completion);
            }
        }

        await CompleteResumeAsync(workerId, reservation, completion!, worker, "operator", ct)
            .ConfigureAwait(false);
        return (true, shouldResume);
    }

    public double? TouchOwner(string workerId, int? leaseS = null)
    {
        double? expiration;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null || st.HijackOwner is null)
            {
                return null;
            }

            var ttl = leaseS is null ? _dashboardLeaseS : ClampDashboardLease(leaseS.Value);
            var exp = _clock.Monotonic() + ttl;
            st.HijackOwnerExpiresAt = exp;
            expiration = exp;
        }

        ArmExpiry(workerId, expiration.Value);
        return expiration;
    }

    public async Task<(bool Released, bool RestActive)> TryReleaseWsAsync(
        string workerId,
        object ws,
        CancellationToken ct = default)
    {
        var reservation = "dashboard-release-resume-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? queuedTransition = null;
        IWorkerWs? worker = null;
        var restActive = false;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    var ownsTransition = st is not null
                        && completion is not null
                        && st.HijackPending == reservation
                        && ReferenceEquals(st.DisconnectResumeCompletion, completion.Task);
                    if (!ownsTransition
                        && (st is null
                            || !_hub.IsDashboardHijackActive(st)
                            || !ReferenceEquals(st.HijackOwner, ws)))
                    {
                        var existingRestActive = st is not null && _hub.HasValidRestLease(st);
                        return (false, existingRestActive);
                    }
                    if (st is null) return (false, false);
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        queuedTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= queuedTransition.Completion;
                        pendingCompletion = queuedTransition.Activated.Task;
                    }
                    else
                    {
                        completion ??= ReserveLifecycleTransition(st, reservation);
                        if (st.InputSendPending is not null)
                        {
                            pendingCompletion = st.InputSendPending.Completion.Task;
                        }
                        else
                        {
                            st.HijackOwner = null;
                            st.HijackOwnerExpiresAt = null;
                            restActive = _hub.HasValidRestLease(st);
                            worker = restActive ? null : st.WorkerWs;
                            break;
                        }
                    }
                }

                await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
            }
            transitionReady = true;
        }
        finally
        {
            if (!transitionReady && completion is not null)
            {
                CompleteTransitionReservation(workerId, reservation, completion);
            }
        }

        await CompleteResumeAsync(workerId, reservation, completion!, worker, "dashboard", ct)
            .ConfigureAwait(false);
        return (true, restActive);
    }

    public bool CheckValid(string workerId, string hijackId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            return st?.HijackSession is not null
                   && st.HijackSession.HijackId == hijackId
                   && st.HijackSession.LeaseExpiresAt > _clock.Monotonic();
        }
    }

    /// <summary>Atomically authorize REST input and reserve its exact lease generation and worker.</summary>
    public async Task<(bool Ok, string Reason)> SendRestInputAsync(
        string workerId,
        string hijackId,
        string keys,
        CancellationToken ct = default)
    {
        PendingInputSend? pending = null;
        while (true)
        {
            Task? pendingCompletion = null;
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is null) return (false, "invalid_hijack");
                if (st.HijackPending is not null
                    && st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion)
                {
                    pendingCompletion = lifecycleCompletion;
                }
                else if (st.HijackSession is null
                    || st.HijackSession.HijackId != hijackId
                    || st.HijackSession.LeaseExpiresAt <= _clock.Monotonic()
                    || st.HijackPending is not null)
                {
                    return (false, "invalid_hijack");
                }
                else if (st.WorkerWs is null)
                {
                    return (false, "no_worker");
                }
                else if (st.InputSendPending is not null)
                {
                    pendingCompletion = st.InputSendPending.Completion.Task;
                }
                else
                {
                    pending = NewInputReservation(
                        st.WorkerWs,
                        restHijackId: hijackId,
                        dashboardOwner: null,
                        dashboardOwnershipVersion: null);
                    st.InputSendPending = pending;
                    break;
                }
            }

            await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
        }

        return await DeliverReservedInputAsync(workerId, pending!, keys, ct).ConfigureAwait(false)
            ? (true, "")
            : (false, "send_failed");
    }

    /// <summary>Atomically authorize browser input and reserve its exact owner generation and worker.</summary>
    public async Task<bool> SendBrowserInputAsync(
        string workerId,
        object browser,
        string text,
        CancellationToken ct = default)
    {
        PendingInputSend? pending = null;
        double? expiration = null;
        while (true)
        {
            Task? pendingCompletion = null;
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is null) return false;
                if (st.HijackPending is not null
                    && st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion)
                {
                    pendingCompletion = lifecycleCompletion;
                }
                else if (st.WorkerWs is null
                    || !st.Browsers.ContainsKey(browser)
                    || browser is IAbortableBrowserWs { IsActive: false }
                    || st.HijackPending is not null
                    || !_hub.CanSendInput(st, browser))
                {
                    return false;
                }
                else if (st.InputSendPending is not null)
                {
                    pendingCompletion = st.InputSendPending.Completion.Task;
                }
                else
                {
                    if (_hub.IsDashboardHijackActive(st) && ReferenceEquals(st.HijackOwner, browser))
                    {
                        st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
                        expiration = st.HijackOwnerExpiresAt;
                    }

                    pending = NewInputReservation(
                        st.WorkerWs,
                        restHijackId: null,
                        dashboardOwner: browser,
                        dashboardOwnershipVersion: st.HijackOwnershipVersion);
                    st.InputSendPending = pending;
                    break;
                }
            }

            await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
        }

        if (expiration is not null) ArmExpiry(workerId, expiration.Value);
        return await DeliverReservedInputAsync(workerId, pending!, text, ct).ConfigureAwait(false);
    }

    private static PendingInputSend NewInputReservation(
        IWorkerWs worker,
        string? restHijackId,
        object? dashboardOwner,
        long? dashboardOwnershipVersion) =>
        new()
        {
            Reservation = "input-send-" + Guid.NewGuid().ToString("N"),
            Worker = worker,
            RestHijackId = restHijackId,
            DashboardOwner = dashboardOwner,
            DashboardOwnershipVersion = dashboardOwnershipVersion,
            Completion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously),
        };

    private async Task<bool> DeliverReservedInputAsync(
        string workerId,
        PendingInputSend pending,
        string text,
        CancellationToken ct)
    {
        var sent = false;
        try
        {
            if (pending.Worker is IAbortableBrowserWs { IsActive: false }) return false;
            await pending.Worker.SendTextAsync(text, ct).ConfigureAwait(false);
            sent = pending.Worker is not IAbortableBrowserWs { IsActive: false };
            return sent;
        }
        catch
        {
            return false;
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null && ReferenceEquals(st.InputSendPending, pending))
                {
                    st.InputSendPending = null;
                    if (sent && ReferenceEquals(st.WorkerWs, pending.Worker))
                    {
                        st.LastActivityAt = _clock.Monotonic();
                    }
                }
            }
            // Always release lifecycle transitions, including cancellation and send failure.
            pending.Completion.TrySetResult();
        }
    }

    public bool PrepareBrowserInput(string workerId, object ws)
    {
        double? expiration = null;
        bool allowed;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null
                || !st.Browsers.ContainsKey(ws)
                || ws is IAbortableBrowserWs { IsActive: false }) return false;
            allowed = _hub.CanSendInput(st, ws);
            if (_hub.IsDashboardHijackActive(st) && ReferenceEquals(st.HijackOwner, ws))
            {
                st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
                expiration = st.HijackOwnerExpiresAt;
            }
        }

        if (expiration is not null) ArmExpiry(workerId, expiration.Value);
        return allowed;
    }

    public async Task<(bool BrowserExpired, bool RestExpired)> CleanupExpiredAsync(
        string workerId,
        CancellationToken ct = default)
    {
        var reservation = "expiry-release-resume-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? queuedTransition = null;
        IWorkerWs? worker = null;
        var browserExpired = false;
        var restExpired = false;
        var publishOwnershipLoss = false;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    if (st is null) return (false, false);
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        queuedTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= queuedTransition.Completion;
                        pendingCompletion = queuedTransition.Activated.Task;
                    }
                    else
                    {
                        var now = _clock.Monotonic();
                        var lease = st.Lease();
                        var (rest, dash) = lease.Expire(now);
                        restExpired = rest;
                        browserExpired = dash;
                        if (!rest && !dash)
                        {
                            if (completion is null) return (false, false);
                            worker = null;
                            break;
                        }
                        if (lease.Ws is not null || lease.Session is not null)
                        {
                            st.ApplyLease(lease);
                            if (completion is null) return (dash, rest);
                            worker = null;
                            break;
                        }
                        if (st.HijackPending is not null && completion is null)
                        {
                            if (st.PendingPauseReservation == st.HijackPending)
                            {
                                st.PendingPauseObligation = st.HijackPending;
                            }
                            return (dash, rest);
                        }
                        completion ??= ReserveLifecycleTransition(st, reservation);
                        if (st.InputSendPending is not null)
                        {
                            pendingCompletion = st.InputSendPending.Completion.Task;
                        }
                        else
                        {
                            st.ApplyLease(lease);
                            browserExpired = dash;
                            restExpired = rest;
                            publishOwnershipLoss = true;
                            worker = st.WorkerWs;
                            break;
                        }
                    }
                }

                await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
            }
            transitionReady = true;
        }
        finally
        {
            if (!transitionReady && completion is not null)
            {
                CompleteTransitionReservation(workerId, reservation, completion);
            }
        }

        await CompleteResumeAsync(
                workerId,
                reservation,
                completion!,
                worker,
                "lease-expired",
                ct,
                publishOwnershipLoss ? () => PublishOwnershipLostAsync(workerId) : null)
            .ConfigureAwait(false);
        return (browserExpired, restExpired);
    }

    public async Task<(bool Released, string Owner)> ForceReleaseAsync(
        string workerId,
        CancellationToken ct = default)
    {
        var owner = "server-forced";
        var reservation = "forced-release-resume-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? queuedTransition = null;
        IWorkerWs? worker = null;
        var had = false;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    if (st is null) return (false, owner);
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        queuedTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= queuedTransition.Completion;
                        pendingCompletion = queuedTransition.Activated.Task;
                    }
                    else if (st.InputSendPending is not null)
                    {
                        completion ??= ReserveLifecycleTransition(st, reservation);
                        pendingCompletion = st.InputSendPending.Completion.Task;
                    }
                    else
                    {
                        if (st.HijackSession is not null)
                        {
                            owner = st.HijackSession.Owner;
                            st.HijackSession = null;
                            had = true;
                        }
                        if (_hub.IsDashboardHijackActive(st)) had = true;
                        st.HijackOwner = null;
                        st.HijackOwnerExpiresAt = null;

                        if (st.PendingDashboardBrowser is not null)
                        {
                            var canceledReservation = st.PendingPauseReservation;
                            if (st.HijackPending == canceledReservation)
                            {
                                st.HijackPending = null;
                            }
                            if (st.PendingPauseReservation == canceledReservation)
                            {
                                st.PendingPauseReservation = null;
                            }
                            st.PendingDashboardBrowser = null;
                            st.PendingDashboardOwnershipVersion = null;
                        }
                        else if (st.DisconnectResumeCompletion is not { IsCompleted: false })
                        {
                            var canceledReservation = st.HijackPending;
                            st.HijackPending = null;
                            if (st.PendingPauseReservation == canceledReservation)
                            {
                                st.PendingPauseReservation = null;
                            }
                        }

                        if (!had || st.WorkerWs is null)
                        {
                            if (completion is null) return (had, owner);
                            worker = null;
                            break;
                        }
                        st.PendingPauseObligation = null;
                        completion ??= ReserveLifecycleTransition(st, reservation);
                        worker = st.WorkerWs;
                        break;
                    }
                }

                await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
            }
            transitionReady = true;
        }
        finally
        {
            if (!transitionReady && completion is not null)
            {
                CompleteTransitionReservation(workerId, reservation, completion);
            }
        }

        await CompleteResumeAsync(workerId, reservation, completion!, worker, owner, ct)
            .ConfigureAwait(false);
        return (had, owner);
    }

    private static TaskCompletionSource ReserveLifecycleTransition(
        WorkerTermState st,
        string reservation) =>
        LifecycleTransitionCoordinator.ReserveActive(st, reservation).Completion;

    private void CompleteTransitionReservation(
        string workerId,
        string reservation,
        TaskCompletionSource completion)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            var transition = st is null
                ? null
                : FindLifecycleTransition(st, reservation, completion);
            if (st is not null && transition is not null)
            {
                LifecycleTransitionCoordinator.Complete(st, transition);
            }
        }
        completion.TrySetResult();
    }

    private async Task CompleteResumeAsync(
        string workerId,
        string reservation,
        TaskCompletionSource completion,
        IWorkerWs? worker,
        string owner,
        CancellationToken ct,
        Func<Task>? beforeReservationRelease = null)
    {
        var sent = false;
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
        bounded.CancelAfter(_hub.ResumeSendTimeout);
        try
        {
            if (worker is null) return;
            if (worker is IAbortableBrowserWs { IsActive: false }) return;
            var encoded = ControlChannelCodec.EncodeControlFrame(ResumeFrame(owner, _clock.Wall()));
            await worker.SendTextAsync(encoded, bounded.Token)
                .WaitAsync(_hub.ResumeSendTimeout, ct).ConfigureAwait(false);
            sent = worker is not IAbortableBrowserWs { IsActive: false };
        }
        catch
        {
            // A failed resume makes this captured transport unfit for a new lease.
        }
        finally
        {
            if (beforeReservationRelease is not null)
            {
                await beforeReservationRelease().ConfigureAwait(false);
            }
            if (!sent && worker is IAbortableBrowserWs abortable) abortable.Abort();
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                // Abortable server transports deregister themselves from their
                // receive-loop finally block, which also publishes offline
                // state. Only a non-abortable failed transport must be removed
                // here directly.
                if (!sent
                    && worker is not IAbortableBrowserWs
                    && st is not null
                    && ReferenceEquals(st.WorkerWs, worker))
                {
                    st.WorkerWs = null;
                }
                var transition = st is null
                    ? null
                    : FindLifecycleTransition(st, reservation, completion);
                if (st is not null && transition is not null)
                {
                    LifecycleTransitionCoordinator.Complete(st, transition);
                }
            }
            completion.TrySetResult();
        }
    }

    private static PendingLifecycleTransition? FindLifecycleTransition(
        WorkerTermState st,
        string reservation,
        TaskCompletionSource completion)
    {
        if (st.ActiveLifecycleTransition is { } active
            && active.Reservation == reservation
            && ReferenceEquals(active.Completion, completion))
        {
            return active;
        }
        return st.LifecycleTransitionQueue.FirstOrDefault(candidate =>
            candidate.Reservation == reservation
            && ReferenceEquals(candidate.Completion, completion));
    }

    private async Task PublishOwnershipLostAsync(string workerId)
    {
        try
        {
            _hub.NotifyHijackChanged(workerId, false, null);
        }
        catch
        {
            // Publication failures must not strand the expiry transition fence.
        }

        try
        {
            await _hub.BroadcastHijackStateAsync(workerId, CancellationToken.None)
                .ConfigureAwait(false);
        }
        catch
        {
            // A failed peer must not strand the expiry transition fence.
        }
    }

    private void ArmExpiry(string workerId, double expiration)
    {
        // ManualClock.SleepAsync advances test time instead of waiting. Existing
        // manual-clock callers drive cleanup explicitly; arming it here would
        // mutate their clock merely because a lease was created.
        if (_clock is ManualClock) return;
        _ = Task.Run(async () =>
        {
            try
            {
                await _clock.SleepAsync(Math.Max(0, expiration - _clock.Monotonic()))
                    .ConfigureAwait(false);
                await CleanupExpiredAsync(workerId, CancellationToken.None).ConfigureAwait(false);
            }
            catch
            {
                // A newer arm or explicit cleanup will recheck the current state.
            }
        });
    }
}
