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
    bool NotifyHijackChanged(OwnershipPublicationToken token);
    /// <summary>Authoritative bound for worker-bound control and input writes.</summary>
    TimeSpan ResumeSendTimeout { get; }
    Task<(bool Ok, Exception? Error)> SendWorkerAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default);
    Task<(bool Reconciled, bool WasHijacked)> ReconcileWorkerDisconnectAsync(
        string workerId,
        IWorkerWs worker);
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

    private sealed record DashboardAcquireResult(
        bool Ok,
        string Reason,
        OwnershipPublicationToken? Publication = null);

    private sealed record DashboardReleaseResult(
        bool Released,
        bool RestActive,
        OwnershipPublicationToken? Publication = null);

    private sealed record DashboardRestoreResult(
        bool Restored,
        OwnershipPublicationToken? Publication = null);

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
        var result = await TryAcquireRestCoreAsync(
            workerId, owner, leaseS, hijackId, now, ct).ConfigureAwait(false);
        if (result.Publication is not null)
        {
            _hub.NotifyHijackChanged(result.Publication);
        }
        return (result.Ok, result.Reason);
    }

    private async Task<(bool Ok, string Reason, OwnershipPublicationToken? Publication)>
        TryAcquireRestCoreAsync(
        string workerId,
        string owner,
        int leaseS,
        string hijackId,
        double now,
        CancellationToken ct)
    {
        var reservation = "rest-pause-" + Guid.NewGuid().ToString("N");
        IWorkerWs? workerWs;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null || st.WorkerWs is null)
            {
                return (false, "no_worker", null);
            }

            if (st.InputMode == InputModes.Open)
            {
                return (false, "open_mode", null);
            }

            if (_hub.IsDashboardHijackActive(st)
                || _hub.HasValidRestLease(st)
                || st.HijackPending is not null
                || st.InputSendPending is not null)
            {
                return (false, "already_hijacked", null);
            }

            workerWs = st.WorkerWs;
            st.HijackPending = reservation;
            st.PendingPauseReservation = reservation;
            st.PendingPauseCompletion ??= NewPauseCompletion();
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
                await ReconcileFailedWorkerSendAsync(workerId, workerWs).ConfigureAwait(false);
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

                return (false, "no_worker", null);
            }

            var committed = false;
            OwnershipPublicationToken? publication = null;
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
                    publication = OwnershipPublicationToken.RestHeld(
                        workerId, st.HijackOwnershipVersion, hijackId, owner);
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
                    workerId,
                    reservation,
                    workerWs,
                    pausePossiblyLanded: true)
                    .ConfigureAwait(false);
                return (false, "no_worker", null);
            }

            ArmExpiry(workerId, now + leaseS);
            return (true, "", publication);
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null) ClearPauseReservation(st, reservation);
            }
            CompletePauseSequenceIfIdle(workerId);
        }
    }

    public (bool Ok, string Reason) TryAcquireWs(string workerId, object ws)
    {
        var result = TryAcquireWsCore(workerId, ws);
        if (result.Publication is not null)
        {
            _hub.NotifyHijackChanged(result.Publication);
        }
        return (result.Ok, result.Reason);
    }

    private DashboardAcquireResult TryAcquireWsCore(string workerId, object ws)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null || st.WorkerWs is null)
            {
                return new(false, "no_worker");
            }

            if (!st.Browsers.ContainsKey(ws)
                || ws is IAbortableBrowserWs { IsActive: false })
            {
                return new(false, "inactive_browser");
            }

            // HijackPending: REST two-phase reserve — treat as already taken so
            // the dashboard WS cannot dual-own during the pause I/O window.
            if (_hub.IsDashboardHijackActive(st)
                || _hub.HasValidRestLease(st)
                || st.HijackPending is not null
                || st.InputSendPending is not null)
            {
                return new(false, "already_hijacked");
            }

            st.HijackOwner = ws;
            st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            st.HijackOwnershipVersion++;
            var publication = OwnershipPublicationToken.DashboardHeld(
                workerId, st.HijackOwnershipVersion, ws);
            ArmExpiry(workerId, st.HijackOwnerExpiresAt.Value);
            return new(true, "", publication);
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
        var result = await TryAcquireWsCoreAsync(
            workerId, ws, ownershipVersion, ct).ConfigureAwait(false);
        if (result.Publication is not null)
        {
            _hub.NotifyHijackChanged(result.Publication);
        }
        return (result.Ok, result.Reason);
    }

    private async Task<DashboardAcquireResult> TryAcquireWsCoreAsync(
        string workerId,
        object ws,
        long? ownershipVersion,
        CancellationToken ct)
    {
        var reservation = "dashboard-pause-" + Guid.NewGuid().ToString("N");
        IWorkerWs workerWs;

        while (true)
        {
            Task? disconnectResume = null;
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st?.WorkerWs is null) return new(false, "no_worker");
                if (!st.Browsers.ContainsKey(ws)
                    || ws is IAbortableBrowserWs { IsActive: false })
                {
                    return new(false, "inactive_browser");
                }

                if (ownershipVersion is { } expected
                    && st.HijackOwnershipVersion != expected)
                {
                    return new(false, "ownership_changed");
                }

                if (st.DisconnectResumeCompletion is { IsCompleted: false } completion)
                {
                    if (ownershipVersion == st.DisconnectResumeOwnershipVersion)
                    {
                        disconnectResume = completion;
                    }
                    else
                    {
                        return new(false, "already_hijacked");
                    }
                }
                else if (_hub.IsDashboardHijackActive(st)
                    || _hub.HasValidRestLease(st)
                    || st.HijackPending is not null
                    || st.InputSendPending is not null)
                {
                    return new(false, "already_hijacked");
                }
                else
                {
                    workerWs = st.WorkerWs;
                    st.HijackPending = reservation;
                    st.PendingPauseReservation = reservation;
                    st.PendingPauseCompletion ??= NewPauseCompletion();
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
                await ReconcileFailedWorkerSendAsync(workerId, workerWs).ConfigureAwait(false);
                if (delivery.Error is OperationCanceledException canceled) throw canceled;
                return new(false, "no_worker");
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
                    var publication = OwnershipPublicationToken.DashboardHeld(
                        workerId, st.HijackOwnershipVersion, ws);
                    if (st.PendingPauseObligation == reservation)
                    {
                        st.PendingPauseObligation = null;
                    }
                    ClearPauseReservation(st, reservation);
                    ArmExpiry(workerId, st.HijackOwnerExpiresAt.Value);
                    return new(true, "", publication);
                }
            }

            await ResolvePauseObligationAsync(
                workerId, reservation, workerWs, pauseLanded)
                .ConfigureAwait(false);
            return new(false, "inactive_browser");
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null) ClearPauseReservation(st, reservation);
            }
            CompletePauseSequenceIfIdle(workerId);
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

    private async Task<PauseDeliveryResult> SendPauseAsync(
        IWorkerWs worker,
        string encodedPause,
        CancellationToken ct)
    {
        if (worker is IAbortableBrowserWs { IsActive: false })
        {
            return new PauseDeliveryResult(PauseDeliveryOutcome.NotDelivered);
        }

        Task? sendTask = null;
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
        bounded.CancelAfter(_hub.ResumeSendTimeout);
        try
        {
            sendTask = worker.SendTextAsync(encodedPause, bounded.Token);
            await sendTask.WaitAsync(_hub.ResumeSendTimeout, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException ex)
        {
            ObserveEventualSendFault(sendTask);
            return new PauseDeliveryResult(PauseDeliveryOutcome.PossiblyDelivered, ex);
        }
        catch (Exception ex)
        {
            ObserveEventualSendFault(sendTask);
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

        var resumed = false;
        Task? sendTask = null;
        using var bounded = new CancellationTokenSource(_hub.ResumeSendTimeout);
        try
        {
            var encodedResume = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "control",
                ["action"] = "resume",
                ["source"] = "dashboard",
                ["ts"] = _clock.Wall(),
            });
            sendTask = pausedWorker.SendTextAsync(encodedResume, bounded.Token);
            await sendTask.WaitAsync(_hub.ResumeSendTimeout, CancellationToken.None)
                .ConfigureAwait(false);
            resumed = pausedWorker is not IAbortableBrowserWs { IsActive: false };
        }
        catch
        {
            ObserveEventualSendFault(sendTask);
        }
        finally
        {
            if (!resumed)
            {
                await ReconcileFailedWorkerSendAsync(workerId, pausedWorker).ConfigureAwait(false);
            }
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

    private async Task<bool> ReconcileFailedWorkerSendAsync(string workerId, IWorkerWs worker)
    {
        if (worker is IAbortableBrowserWs abortable)
        {
            try
            {
                abortable.Abort();
            }
            catch
            {
                // Identity-gated reconciliation remains authoritative.
            }
        }
        var (reconciled, _) = await _hub.ReconcileWorkerDisconnectAsync(workerId, worker)
            .ConfigureAwait(false);
        return reconciled;
    }

    private void CompletePauseSequenceIfIdle(string workerId)
    {
        TaskCompletionSource? completion = null;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is not null
                && st.PendingPauseReservation is null
                && st.PendingPauseObligation is null)
            {
                if (st.ActiveLifecycleTransition is { } activeTransition)
                {
                    st.HijackPending = activeTransition.Reservation;
                }
                completion = st.PendingPauseCompletion;
                st.PendingPauseCompletion = null;
            }
        }
        completion?.TrySetResult();
    }

    private static TaskCompletionSource NewPauseCompletion() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    /// <summary>Restore the same logical dashboard owner only if no later owner has existed.</summary>
    public bool TryRestoreWsOwnership(string workerId, object ws, long ownershipVersion)
    {
        var result = TryRestoreWsOwnershipCore(workerId, ws, ownershipVersion);
        if (result.Publication is not null)
        {
            _hub.NotifyHijackChanged(result.Publication);
        }
        return result.Restored;
    }

    private DashboardRestoreResult TryRestoreWsOwnershipCore(
        string workerId,
        object ws,
        long ownershipVersion)
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
                return new(false);
            }

            st.HijackOwner = ws;
            st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            var publication = OwnershipPublicationToken.DashboardHeld(
                workerId, st.HijackOwnershipVersion, ws);
            ArmExpiry(workerId, st.HijackOwnerExpiresAt.Value);
            return new(true, publication);
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
        PendingLifecycleTransition? lifecycleTransition = null;
        IWorkerWs? worker = null;
        var shouldResume = false;
        OwnershipPublicationToken? publication = null;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    if (lifecycleTransition?.IsTerminal == true)
                    {
                        lifecycleTransition = null;
                        completion = null;
                    }
                    if (st?.HijackSession is null || st.HijackSession.HijackId != hijackId)
                    {
                        return (false, false);
                    }
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        pendingCompletion = lifecycleTransition.Activated.Task;
                    }
                    else
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.ReserveActive(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        if (st.InputSendPending is not null)
                        {
                            pendingCompletion = Task.WhenAny(
                                st.InputSendPending.Completion.Task,
                                lifecycleTransition.Completion.Task);
                        }
                        else
                        {
                            st.HijackSession = null;
                            st.HijackOwnershipVersion++;
                            publication = OwnershipPublicationToken.Released(
                                workerId, st.HijackOwnershipVersion);
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
        if (publication is not null) _hub.NotifyHijackChanged(publication);
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
        var result = await TryReleaseWsCoreAsync(workerId, ws, ct).ConfigureAwait(false);
        if (result.Publication is not null)
        {
            _hub.NotifyHijackChanged(result.Publication);
        }
        return (result.Released, result.RestActive);
    }

    private async Task<DashboardReleaseResult> TryReleaseWsCoreAsync(
        string workerId,
        object ws,
        CancellationToken ct)
    {
        var reservation = "dashboard-release-resume-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? lifecycleTransition = null;
        IWorkerWs? worker = null;
        var restActive = false;
        OwnershipPublicationToken? publication = null;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    if (lifecycleTransition?.IsTerminal == true)
                    {
                        lifecycleTransition = null;
                        completion = null;
                    }
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
                        return new(false, existingRestActive);
                    }
                    if (st is null) return new(false, false);
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        pendingCompletion = lifecycleTransition.Activated.Task;
                    }
                    else
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.ReserveActive(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        if (st.InputSendPending is not null)
                        {
                            pendingCompletion = Task.WhenAny(
                                st.InputSendPending.Completion.Task,
                                lifecycleTransition.Completion.Task);
                        }
                        else
                        {
                            st.HijackOwner = null;
                            st.HijackOwnerExpiresAt = null;
                            st.HijackOwnershipVersion++;
                            publication = OwnershipPublicationToken.Released(
                                workerId, st.HijackOwnershipVersion);
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
        return new(true, restActive, publication);
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
        var reconcileWorker = false;
        Task? sendTask = null;
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
        bounded.CancelAfter(_hub.ResumeSendTimeout);
        try
        {
            if (pending.Worker is not IAbortableBrowserWs { IsActive: false })
            {
                sendTask = pending.Worker.SendTextAsync(text, bounded.Token);
                await sendTask.WaitAsync(_hub.ResumeSendTimeout, ct).ConfigureAwait(false);
                sent = pending.Worker is not IAbortableBrowserWs { IsActive: false };
            }
        }
        catch
        {
            ObserveEventualSendFault(sendTask);
        }
        finally
        {
            if (!sent && pending.Worker is IAbortableBrowserWs abortable)
            {
                try
                {
                    abortable.Abort();
                }
                catch
                {
                    // Registry fencing below is authoritative even if transport abort fails.
                }
            }
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
                reconcileWorker = !sent;
            }
            try
            {
                if (reconcileWorker)
                {
                    await _hub.ReconcileWorkerDisconnectAsync(workerId, pending.Worker)
                        .ConfigureAwait(false);
                }
            }
            finally
            {
                // Always release lifecycle transitions, including cancellation and send failure.
                pending.Completion.TrySetResult();
            }
        }
        return sent;
    }

    private static void ObserveEventualSendFault(Task? task)
    {
        if (task is null) return;
        _ = task.ContinueWith(
            static completed => _ = completed.Exception,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
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
        PendingLifecycleTransition? lifecycleTransition = null;
        IWorkerWs? worker = null;
        var browserExpired = false;
        var restExpired = false;
        OwnershipPublicationToken? publication = null;
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
                    if (lifecycleTransition?.IsTerminal == true)
                    {
                        lifecycleTransition = null;
                        completion = null;
                    }
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        pendingCompletion = lifecycleTransition.Activated.Task;
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
                        lifecycleTransition ??= LifecycleTransitionCoordinator.ReserveActive(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        if (st.InputSendPending is not null)
                        {
                            pendingCompletion = Task.WhenAny(
                                st.InputSendPending.Completion.Task,
                                lifecycleTransition.Completion.Task);
                        }
                        else
                        {
                            st.ApplyLease(lease);
                            st.HijackOwnershipVersion++;
                            browserExpired = dash;
                            restExpired = rest;
                            publication = OwnershipPublicationToken.Released(
                                workerId, st.HijackOwnershipVersion);
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

        var workerReconciled = await CompleteResumeAsync(
                workerId,
                reservation,
                completion!,
                worker,
                "lease-expired",
                ct)
            .ConfigureAwait(false);
        if (publication is not null)
        {
            _hub.NotifyHijackChanged(publication);
            try
            {
                if (!workerReconciled)
                {
                    await _hub.BroadcastHijackStateAsync(workerId, CancellationToken.None)
                        .ConfigureAwait(false);
                }
            }
            catch
            {
                // Expiry publication is best-effort after lifecycle release.
            }
        }
        return (browserExpired, restExpired);
    }

    public async Task<(bool Released, string Owner)> ForceReleaseAsync(
        string workerId,
        CancellationToken ct = default)
    {
        var owner = "server-forced";
        var reservation = "forced-release-resume-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? lifecycleTransition = null;
        IWorkerWs? worker = null;
        var had = false;
        OwnershipPublicationToken? publication = null;
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
                    if (lifecycleTransition?.IsTerminal == true)
                    {
                        lifecycleTransition = null;
                        completion = null;
                    }
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        pendingCompletion = lifecycleTransition.Activated.Task;
                    }
                    else if (st.InputSendPending is not null)
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.ReserveActive(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
                        pendingCompletion = Task.WhenAny(
                            st.InputSendPending.Completion.Task,
                            lifecycleTransition.Completion.Task);
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
                        if (had)
                        {
                            st.HijackOwnershipVersion++;
                            publication = OwnershipPublicationToken.Released(
                                workerId, st.HijackOwnershipVersion);
                        }

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
                            if (!had && completion is null) return (false, owner);
                            worker = null;
                            break;
                        }
                        st.PendingPauseObligation = null;
                        lifecycleTransition ??= LifecycleTransitionCoordinator.ReserveActive(
                            st, reservation);
                        completion ??= lifecycleTransition.Completion;
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

        if (completion is not null)
        {
            await CompleteResumeAsync(workerId, reservation, completion, worker, owner, ct)
                .ConfigureAwait(false);
        }
        if (publication is not null) _hub.NotifyHijackChanged(publication);
        return (had, owner);
    }

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

    private async Task<bool> CompleteResumeAsync(
        string workerId,
        string reservation,
        TaskCompletionSource completion,
        IWorkerWs? worker,
        string owner,
        CancellationToken ct)
    {
        var sent = false;
        var reconciled = false;
        Task? sendTask = null;
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
        bounded.CancelAfter(_hub.ResumeSendTimeout);
        try
        {
            if (worker is not null
                && worker is not IAbortableBrowserWs { IsActive: false })
            {
                var encoded = ControlChannelCodec.EncodeControlFrame(
                    ResumeFrame(owner, _clock.Wall()));
                sendTask = worker.SendTextAsync(encoded, bounded.Token);
                await sendTask.WaitAsync(_hub.ResumeSendTimeout, ct).ConfigureAwait(false);
                sent = worker is not IAbortableBrowserWs { IsActive: false };
            }
        }
        catch
        {
            ObserveEventualSendFault(sendTask);
        }
        finally
        {
            if (!sent && worker is not null)
            {
                reconciled = await ReconcileFailedWorkerSendAsync(workerId, worker)
                    .ConfigureAwait(false);
            }
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
        return reconciled;
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
