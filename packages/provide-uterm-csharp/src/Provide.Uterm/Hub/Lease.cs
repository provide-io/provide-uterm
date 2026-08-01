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
public sealed partial class HijackLeaseManager
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
}
