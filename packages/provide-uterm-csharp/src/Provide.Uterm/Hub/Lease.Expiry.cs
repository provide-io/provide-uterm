//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Hub;

// HijackLeaseManager: browser input delivery, expiry cleanup, force release, and lifecycle transitions.
public sealed partial class HijackLeaseManager
{
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
                        restLeaseExpiresAt: null,
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
        double? restLeaseExpiresAt,
        object? dashboardOwner,
        long? dashboardOwnershipVersion) =>
        new()
        {
            Reservation = "input-send-" + Guid.NewGuid().ToString("N"),
            Worker = worker,
            RestHijackId = restHijackId,
            RestLeaseExpiresAt = restLeaseExpiresAt,
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
                        if (pending.RestHijackId is not null
                            && st.HijackSession?.HijackId == pending.RestHijackId)
                        {
                            pending.RestLeaseExpiresAt = st.HijackSession.LeaseExpiresAt;
                        }
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
