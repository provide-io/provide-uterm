//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Hub;

/// <summary>Browser admission refusal with its WebSocket close code.</summary>
public sealed class BrowserRegistrationException : Exception
{
    public BrowserRegistrationException(int closeCode, string reason) : base(reason) => CloseCode = closeCode;

    public int CloseCode { get; }
}

/// <summary>Worker/browser registration and REST rate-limit facade.</summary>
public sealed class ConnectionManager
{
    private enum BrowserSendOutcome
    {
        Sent,
        PeerFailed,
        CallerCancelled,
    }

    private sealed record BrowserCleanupResult(
        long? OwnershipVersion,
        OwnershipPublicationToken? Publication = null);

    private readonly TermHub _hub;
    private Action<string>? _markWorkerOffline;

    internal Func<string, Task>? AfterReplacementPauseFenceWait { get; set; }

    internal ConnectionManager(TermHub hub) => _hub = hub;

    internal void ConfigureWorkerOfflineMarker(Action<string> marker) =>
        _markWorkerOffline = marker;

    public bool AllowRestAcquireFor(string clientId) => _hub.Limiter.AllowRestAcquire(clientId);

    public bool AllowRestSendFor(string clientId) => _hub.Limiter.AllowRestSend(clientId);

    public bool RegisterWorker(string workerId, IWorkerWs ws) =>
        RegisterWorkerAsync(workerId, ws).GetAwaiter().GetResult();

    public async Task<bool> RegisterWorkerAsync(
        string workerId,
        IWorkerWs ws,
        CancellationToken ct = default)
    {
        var reservation = "worker-replacement-" + Guid.NewGuid().ToString("N");
        TaskCompletionSource? completion = null;
        PendingLifecycleTransition? lifecycleTransition = null;
        IWorkerWs? predecessor = null;
        var mustResume = false;
        var ownershipLost = false;
        OwnershipPublicationToken? ownershipPublication = null;
        var replacementReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                var waitedForPause = false;
                lock (_hub.SharedLock)
                {
                    if (_hub.Registry.Count >= _hub.MaxWorkers && !_hub.Registry.Contains(workerId))
                    {
                        return false;
                    }

                    var st = _hub.State.GetOrCreate(workerId);
                    if (lifecycleTransition?.IsTerminal == true)
                    {
                        lifecycleTransition = null;
                        completion = null;
                    }
                    if (ReferenceEquals(st.WorkerWs, ws)) return true;
                    if (st.DisconnectResumeCompletion is { IsCompleted: false } lifecycleCompletion
                        && (completion is null
                            || !ReferenceEquals(lifecycleCompletion, completion.Task)))
                    {
                        lifecycleTransition ??= LifecycleTransitionCoordinator.EnqueueSuccessor(
                            st, reservation, preserveOnWorkerClear: true);
                        completion ??= lifecycleTransition.Completion;
                        pendingCompletion = lifecycleTransition.Activated.Task;
                    }
                    else if (st.PendingPauseCompletion is { Task.IsCompleted: false } pauseCompletion)
                    {
                        if (completion is null)
                        {
                            lifecycleTransition = LifecycleTransitionCoordinator.ReserveActive(
                                st, reservation, preserveOnWorkerClear: true);
                            completion = lifecycleTransition.Completion;
                            RestorePendingPauseReservation(st);
                        }
                        pendingCompletion = pauseCompletion.Task;
                        waitedForPause = true;
                    }
                    else if (st.InputSendPending is not null)
                    {
                        if (completion is null)
                        {
                            lifecycleTransition = LifecycleTransitionCoordinator.ReserveActive(
                                st, reservation, preserveOnWorkerClear: true);
                            completion = lifecycleTransition.Completion;
                        }
                        pendingCompletion = Task.WhenAny(
                            st.InputSendPending.Completion.Task,
                            lifecycleTransition!.Completion.Task);
                    }
                    else
                    {
                        predecessor = st.WorkerWs;
                        if (predecessor is not null)
                        {
                            mustResume = st.HijackSession is not null
                                || st.HijackOwner is not null
                                || st.PendingPauseObligation is not null;
                            ownershipLost = st.HijackSession is not null || st.HijackOwner is not null;
                            st.HijackSession = null;
                            st.HijackOwner = null;
                            st.HijackOwnerExpiresAt = null;
                            st.PendingDashboardBrowser = null;
                            st.PendingDashboardOwnershipVersion = null;
                            st.PendingPauseReservation = null;
                            st.PendingPauseObligation = null;
                            st.HijackOwnershipVersion++;
                            if (ownershipLost)
                            {
                                ownershipPublication = OwnershipPublicationToken.Released(
                                    workerId, st.HijackOwnershipVersion);
                            }
                            if (completion is null)
                            {
                                lifecycleTransition = LifecycleTransitionCoordinator.ReserveActive(
                                    st, reservation, preserveOnWorkerClear: true);
                                completion = lifecycleTransition.Completion;
                            }
                        }
                        st.WorkerWs = ws;
                        st.LastActivityAt = _hub.Clock.Monotonic();
                        break;
                    }
                }

                await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
                if (waitedForPause
                    && AfterReplacementPauseFenceWait is { } afterPauseWait)
                {
                    await afterPauseWait(workerId).ConfigureAwait(false);
                }
            }
            replacementReady = true;
        }
        finally
        {
            if (!replacementReady && completion is not null)
            {
                CompleteWorkerReplacementReservation(workerId, reservation, completion);
            }
        }

        if (predecessor is null)
        {
            if (completion is not null)
            {
                CompleteWorkerReplacementReservation(workerId, reservation, completion);
            }
            return true;
        }
        try
        {
            if (mustResume)
            {
                using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
                bounded.CancelAfter(_hub.BrowserSendTimeout);
                try
                {
                    var encoded = ControlChannelCodec.EncodeControlFrame(
                        HijackLeaseManager.ResumeFrame("worker-replaced", _hub.Clock.Wall()));
                    await predecessor.SendTextAsync(encoded, bounded.Token)
                        .WaitAsync(_hub.BrowserSendTimeout, ct).ConfigureAwait(false);
                }
                catch
                {
                    // Replacement remains authoritative even if the displaced transport cannot resume.
                }
            }
        }
        finally
        {
            if (ownershipPublication is not null)
            {
                await PublishOwnershipLostAsync(ownershipPublication).ConfigureAwait(false);
            }
            if (predecessor is IAbortableBrowserWs abortable) abortable.Abort();
            lock (_hub.SharedLock)
            {
                var st = _hub.Registry.Get(workerId);
                var transition = st is null || completion is null
                    ? null
                    : FindLifecycleTransition(st, reservation, completion);
                if (st is not null && transition is not null)
                {
                    LifecycleTransitionCoordinator.Complete(st, transition);
                }
            }
            completion?.TrySetResult();
        }
        return true;
    }

    private void CompleteWorkerReplacementReservation(
        string workerId,
        string reservation,
        TaskCompletionSource completion)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            var transition = st is null
                ? null
                : FindLifecycleTransition(st, reservation, completion);
            if (st is not null && transition is not null)
            {
                LifecycleTransitionCoordinator.Complete(st, transition);
                RestorePendingPauseReservation(st);
            }
        }
        completion.TrySetResult();
    }

    private static void RestorePendingPauseReservation(WorkerTermState st)
    {
        if (st.PendingPauseCompletion is { Task.IsCompleted: false }
            && st.PendingPauseReservation is not null)
        {
            st.HijackPending = st.PendingPauseReservation;
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

    private async Task PublishOwnershipLostAsync(OwnershipPublicationToken publication)
    {
        try
        {
            _hub.State.NotifyHijackChanged(publication);
        }
        catch
        {
            // Publication failures must not strand the replacement transition fence.
        }

        try
        {
            await BroadcastHijackStateAsync(
                publication.WorkerId, CancellationToken.None).ConfigureAwait(false);
        }
        catch
        {
            // A failed peer must not strand the replacement transition fence.
        }
    }

    public bool IsActiveWorker(string workerId, IWorkerWs ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            return st is not null && ReferenceEquals(st.WorkerWs, ws);
        }
    }

    /// <summary>
    /// Apply a <c>worker_hello</c>: record the announced input mode and the
    /// negotiated protocol version. Port of
    /// <c>ConnectionManager.set_worker_hello</c>.
    /// </summary>
    /// <remarks>
    /// A hello may raise the mode and may never lower a decided one. Two
    /// reasons to refuse and both are needed: a lease is actually held, or
    /// somebody decided the mode through an authenticated route. The second is
    /// the window a lease-only check leaves open — an operator sets
    /// <c>hijack</c> and then acquires, and a hello landing between those steps
    /// reverts the mode, so the acquire is refused for being in open mode and
    /// the operator's only clue is a failure that looks like their own mistake.
    ///
    /// Keyed on whether the hello would actually lower the mode rather than on
    /// its value, so a hello agreeing with a decided <c>open</c> is not a
    /// downgrade. And the decision flag is what makes the rule expressible:
    /// <c>InputMode</c> defaults to <c>hijack</c>, so refusing every lowering
    /// would refuse every worker that legitimately announces <c>open</c>.
    /// </remarks>
    /// <returns>
    /// <c>true</c> when the mode was applied, <c>false</c> for an unknown
    /// worker or a hello that would lower a decided mode.
    /// </returns>
    public bool SetWorkerHello(string workerId, string mode, int? protocolVersion = null) =>
        SetWorkerHelloCore(workerId, null, mode, protocolVersion).Applied;

    public bool SetWorkerHello(
        string workerId,
        IWorkerWs worker,
        string mode,
        int? protocolVersion = null) =>
        SetWorkerHelloCore(workerId, worker, mode, protocolVersion).Applied;

    internal (bool Current, bool Applied) SetWorkerHelloFrame(
        string workerId,
        IWorkerWs worker,
        string mode,
        int? protocolVersion = null) =>
        SetWorkerHelloCore(workerId, worker, mode, protocolVersion);

    private (bool Current, bool Applied) SetWorkerHelloCore(
        string workerId,
        IWorkerWs? expectedWorker,
        string mode,
        int? protocolVersion)
    {
        bool blocked;
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null
                || expectedWorker is not null && !ReferenceEquals(st.WorkerWs, expectedWorker))
            {
                return (false, false);
            }

            var wouldLower = mode == InputModes.Open && st.InputMode == InputModes.Hijack;
            blocked = wouldLower && (st.InputModeSetByOperator || _hub.State.IsHijacked(st));
            if (!blocked)
            {
                st.InputMode = mode;
                if (protocolVersion is not null)
                {
                    st.ProtocolVersion = protocolVersion;
                }
            }
        }

        if (blocked)
        {
            // Named, because a counter cannot be. The server also increments
            // `worker_hello_mode_blocked_total`, which says refusals are
            // happening; this says which worker is causing them, which is what
            // somebody looking at a session stuck in hijack has to know. Same
            // place and same text as the reference
            // (bridge/hub/connection.py:set_worker_hello).
            //
            // Outside the lock: the sink is host code and must not run with the
            // hub's lock held. The decision is captured inside it instead.
            _hub.Log(
                "warning",
                $"worker_hello_mode_blocked worker_id={workerId} — a hello may not lower a decided mode to open");
            // Counted here rather than only at the WebSocket route, because the
            // refusal is this method's decision. Counting it upstream left any
            // other caller of this method logged but uncounted, so the two
            // signals disagreed about how often it happens.
            _hub.Metric("worker_hello_mode_blocked_total", 1);
            return (true, false);
        }

        return (true, true);
    }

    /// <summary>
    /// Linearize acceptance of a decoded worker frame against worker replacement.
    /// A caller may fan out the frame only when this returns <c>true</c>.
    /// </summary>
    public bool TryAcceptWorkerFrame(string workerId, IWorkerWs worker)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            return st is not null && ReferenceEquals(st.WorkerWs, worker);
        }
    }

    public void UpdateLastSnapshot(string workerId, Dictionary<string, object?> snapshot)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return;
            st.LastSnapshot = new Dictionary<string, object?>(snapshot);
            st.LastActivityAt = _hub.Clock.Monotonic();
        }
    }

    public bool UpdateLastSnapshot(
        string workerId,
        IWorkerWs worker,
        Dictionary<string, object?> snapshot)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null || !ReferenceEquals(st.WorkerWs, worker)) return false;
            st.LastSnapshot = new Dictionary<string, object?>(snapshot);
            st.LastActivityAt = _hub.Clock.Monotonic();
            return true;
        }
    }

    /// <summary>Apply a tunnel worker's announced mode only while that transport is current.</summary>
    public bool TrySetWorkerInputMode(string workerId, IWorkerWs worker, string mode)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null || !ReferenceEquals(st.WorkerWs, worker)) return false;
            st.InputMode = mode;
            return true;
        }
    }

    /// <summary>
    /// Append an event and touch activity in the same worker-identity critical section.
    /// </summary>
    public bool TryAppendWorkerEvent(
        string workerId,
        IWorkerWs worker,
        string eventType,
        Dictionary<string, object?> data)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null || !ReferenceEquals(st.WorkerWs, worker)) return false;
            _hub.Router.AppendEvent(workerId, eventType, data);
            st.LastActivityAt = _hub.Clock.Monotonic();
            return true;
        }
    }

    public (bool ShouldBroadcast, bool WasHijacked) DeregisterWorker(string workerId, IWorkerWs ws)
    {
        lock (_hub.SharedLock)
        {
            return DeregisterWorkerLocked(
                workerId, ws, markOffline: false, out _);
        }
    }

    /// <summary>
    /// Reconcile one captured worker transport exactly once. The worker identity
    /// check gates the registry update, lifecycle release, and publications, so
    /// a later receive-loop finalizer cannot tear down a replacement.
    /// </summary>
    public async Task<(bool Reconciled, bool WasHijacked)> ReconcileWorkerDisconnectAsync(
        string workerId,
        IWorkerWs ws)
    {
        (bool Reconciled, bool WasHijacked) result;
        OwnershipPublicationToken? ownershipPublication;
        lock (_hub.SharedLock)
        {
            result = DeregisterWorkerLocked(
                workerId, ws, markOffline: true, out ownershipPublication);
        }
        if (!result.Reconciled) return result;

        try
        {
            await BroadcastToBrowsersAsync(
                workerId,
                new Dictionary<string, object?>
                {
                    ["type"] = "worker_disconnected",
                    ["worker_id"] = workerId,
                    ["ts"] = _hub.Clock.Wall(),
                },
                CancellationToken.None).ConfigureAwait(false);
        }
        catch
        {
            // Teardown publication is best-effort and must not strand successors.
        }

        try
        {
            if (ownershipPublication is not null)
            {
                _hub.State.NotifyHijackChanged(ownershipPublication);
            }
        }
        catch
        {
            // Host callbacks cannot make authoritative teardown fail.
        }

        try
        {
            await BroadcastHijackStateAsync(workerId, CancellationToken.None).ConfigureAwait(false);
        }
        catch
        {
            // Teardown publication is best-effort and must not strand successors.
        }
        return result;
    }

    private (bool Reconciled, bool WasHijacked) DeregisterWorkerLocked(
        string workerId,
        IWorkerWs ws,
        bool markOffline,
        out OwnershipPublicationToken? ownershipPublication)
    {
        var st = _hub.Registry.Get(workerId);
        if (st is null || !ReferenceEquals(st.WorkerWs, ws))
        {
            ownershipPublication = null;
            return (false, false);
        }

        var wasHijacked = st.HijackSession is not null || st.HijackOwner is not null;
        st.WorkerWs = null;
        st.HijackSession = null;
        st.HijackOwner = null;
        st.HijackOwnerExpiresAt = null;
        st.PendingDashboardBrowser = null;
        st.PendingDashboardOwnershipVersion = null;
        st.PendingPauseReservation = null;
        st.PendingPauseObligation = null;
        var pendingPauseCompletion = st.PendingPauseCompletion;
        st.PendingPauseCompletion = null;
        if (wasHijacked)
        {
            st.HijackOwnershipVersion++;
            ownershipPublication = OwnershipPublicationToken.Released(
                workerId, st.HijackOwnershipVersion);
        }
        else
        {
            ownershipPublication = null;
        }
        if (markOffline)
        {
            try
            {
                _markWorkerOffline?.Invoke(workerId);
            }
            catch
            {
                // Host registry failures cannot strand lifecycle successors.
            }
        }
        // Mark the captured worker offline before activating a queued
        // replacement. Its route's subsequent online mark therefore wins.
        LifecycleTransitionCoordinator.Clear(st);
        pendingPauseCompletion?.TrySetResult();
        return (true, wasHijacked);
    }

    public bool DisconnectWorker(string workerId)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st?.WorkerWs is null) return false;
            st.WorkerWs = null;
            return true;
        }
    }

    /// <summary>
    /// End whatever is held on a worker and let it run again — the release
    /// nobody asked for, which is what opening a session does to the lease
    /// that was gating it.
    ///
    /// The reference does not clear the state and stop
    /// (<c>bridge/hub/connection_hijack.py:99-131</c>): it tells the worker to
    /// resume (naming the owner it took the session from, or
    /// <c>server-forced</c> when the holder was a dashboard socket), fires the
    /// hijack-changed callback, and broadcasts the new hijack state to the
    /// browsers. A silent release would leave the worker paused with nobody
    /// left to unpause it and every connected UI still showing a lease that no
    /// longer exists.
    ///
    /// Reports whether anything was actually held, so a caller can tell a
    /// release from a no-op — and "held" is the same expiry-aware predicate
    /// the rest of the hub uses, so an owner whose lease already ran out is
    /// not announced as a release.
    /// </summary>
    public async Task<bool> ForceReleaseHijackAsync(string workerId, CancellationToken ct = default)
    {
        var (had, _) = await _hub.Lease.ForceReleaseAsync(workerId, ct).ConfigureAwait(false);
        if (!had) return false;
        await BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
        return true;
    }

    public Dictionary<string, object?> RegisterBrowser(
        string workerId,
        object ws,
        string role,
        bool deferBroadcast = false,
        string? principalSubjectId = null)
    {
        lock (_hub.SharedLock)
        {
            var countedSubject = string.IsNullOrWhiteSpace(principalSubjectId)
                || principalSubjectId == "anonymous"
                ? null
                : principalSubjectId;
            if (countedSubject is not null)
            {
                var current = _hub.PrincipalBrowserCounts.GetValueOrDefault(countedSubject);
                if (current >= _hub.MaxConnectionsPerPrincipal)
                {
                    throw new BrowserRegistrationException(1008, "too many connections");
                }

                _hub.PrincipalBrowserCounts[countedSubject] = current + 1;
                _hub.BrowserPrincipals[ws] = countedSubject;
            }

            try
            {
                var st = _hub.State.GetOrCreate(workerId);
                st.Browsers[ws] = string.IsNullOrWhiteSpace(role) ? "viewer" : role;
                if (deferBroadcast) _hub.StartupPendingBrowsers.Add(ws);
                st.LastActivityAt = _hub.Clock.Monotonic();
                return new Dictionary<string, object?>
                {
                    ["is_hijacked"] = _hub.State.IsHijacked(st),
                    ["hijacked_by_me"] = _hub.State.IsDashboardHijackActive(st) && ReferenceEquals(st.HijackOwner, ws),
                    ["worker_online"] = st.WorkerWs is not null,
                    ["input_mode"] = st.InputMode,
                    ["role"] = st.Browsers[ws],
                };
            }
            catch
            {
                RollbackBrowserQuota(ws);
                throw;
            }
        }
    }

    public void ActivateBrowserBroadcasts(string workerId, object ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st?.Browsers.ContainsKey(ws) is true) _hub.StartupPendingBrowsers.Remove(ws);
        }
    }

    public Dictionary<string, object?> GetBrowserState(string workerId, object ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId)
                ?? throw new InvalidOperationException("browser worker state is missing");
            return new Dictionary<string, object?>
            {
                ["is_hijacked"] = _hub.State.IsHijacked(st),
                ["hijacked_by_me"] = _hub.State.IsDashboardHijackActive(st)
                    && ReferenceEquals(st.HijackOwner, ws),
                ["worker_online"] = st.WorkerWs is not null,
                ["input_mode"] = st.InputMode,
                ["role"] = st.Browsers.GetValueOrDefault(ws, "viewer"),
            };
        }
    }

    public bool IsBrowserRegistered(string workerId, object ws)
    {
        lock (_hub.SharedLock)
        {
            var registered = _hub.Registry.Get(workerId)?.Browsers.ContainsKey(ws) is true;
            return registered && (ws is not IAbortableBrowserWs browser || browser.IsActive);
        }
    }

    public long? CleanupBrowser(string workerId, object ws, bool retainOwnershipVersion = false)
    {
        var result = CleanupBrowserCore(workerId, ws, retainOwnershipVersion);
        if (result.Publication is not null)
        {
            PublishOwnershipTransition(result.Publication);
        }
        return result.OwnershipVersion;
    }

    private BrowserCleanupResult CleanupBrowserCore(
        string workerId,
        object ws,
        bool retainOwnershipVersion)
    {
        lock (_hub.SharedLock)
        {
            OwnershipPublicationToken? publication = null;
            long? ownershipVersion = _hub.PendingBrowserOwnershipVersions.Remove(ws, out var pendingVersion)
                ? pendingVersion
                : null;
            var st = _hub.Registry.Get(workerId);
            if (st is not null)
            {
                st.Browsers.Remove(ws);
                if (ReferenceEquals(st.PendingDashboardBrowser, ws))
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
                if (ReferenceEquals(st.HijackOwner, ws))
                {
                    if (_hub.State.IsDashboardHijackActive(st))
                    {
                        ownershipVersion = st.HijackOwnershipVersion;
                    }
                    if (ownershipVersion is not null
                        && (st.InputSendPending is not null
                            || st.DisconnectResumeCompletion is { IsCompleted: false }))
                    {
                        if (st.PendingDisconnectTransition is null)
                        {
                            var reservation = "disconnect-resume-" + Guid.NewGuid().ToString("N");
                            st.PendingDisconnectTransition =
                                st.DisconnectResumeCompletion is { IsCompleted: false }
                                    ? LifecycleTransitionCoordinator.EnqueueSuccessor(
                                        st, reservation, ownershipVersion, ws)
                                    : LifecycleTransitionCoordinator.ReserveActive(
                                        st, reservation, ownershipVersion, ws);
                        }
                    }
                    else
                    {
                        st.HijackOwner = null;
                        st.HijackOwnerExpiresAt = null;
                        if (ownershipVersion is not null)
                        {
                            publication = OwnershipPublicationToken.Released(
                                workerId, st.HijackOwnershipVersion);
                        }
                    }
                }
            }

            _hub.StartupPendingBrowsers.Remove(ws);
            RollbackBrowserQuota(ws);
            if (retainOwnershipVersion && ownershipVersion is not null)
            {
                // A broadcast timeout aborts the transport before its receive-loop
                // finally block runs. Preserve the generation for that block so it
                // can resume the worker and mark the matching resume token.
                _hub.PendingBrowserOwnershipVersions[ws] = ownershipVersion.Value;
            }
            return new(ownershipVersion, publication);
        }
    }

    private void RollbackBrowserQuota(object ws)
    {
        if (!_hub.BrowserPrincipals.Remove(ws, out var subjectId)) return;
        var remaining = _hub.PrincipalBrowserCounts.GetValueOrDefault(subjectId) - 1;
        if (remaining <= 0) _hub.PrincipalBrowserCounts.Remove(subjectId);
        else _hub.PrincipalBrowserCounts[subjectId] = remaining;
    }

    public async Task<(bool Ok, Exception? Error)> SendWorkerAsync(
        string workerId,
        Dictionary<string, object?> msg,
        CancellationToken ct = default)
    {
        IWorkerWs? ws;
        lock (_hub.SharedLock)
        {
            ws = _hub.Registry.Get(workerId)?.WorkerWs;
        }

        if (ws is null)
        {
            return (false, null);
        }

        if (ws is IAbortableBrowserWs { IsActive: false })
        {
            return (false, null);
        }

        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(msg);
            await ws.SendTextAsync(encoded, ct).ConfigureAwait(false);
            if (ws is IAbortableBrowserWs { IsActive: false })
            {
                return (false, null);
            }
            return (true, null);
        }
        catch (Exception ex)
        {
            return (false, ex);
        }
    }

    /// <summary>
    /// Resume a worker after dashboard-owner cleanup only while that exact
    /// ownership generation is still the most recent one.
    /// </summary>
    public async Task<(bool Resumed, Exception? Error)> ResumeWorkerIfOwnershipUnchangedAsync(
        string workerId,
        long ownershipVersion,
        Dictionary<string, object?> msg,
        CancellationToken ct = default)
    {
        var reservation = "disconnect-resume-" + Guid.NewGuid().ToString("N");
        PendingLifecycleTransition? transition = null;
        IWorkerWs? worker = null;
        OwnershipPublicationToken? ownershipPublication = null;
        var transitionReady = false;
        try
        {
            while (true)
            {
                Task? pendingCompletion = null;
                lock (_hub.SharedLock)
                {
                    var st = _hub.Registry.Get(workerId);
                    if (st is null) return (false, null);
                    if (transition?.IsTerminal == true) return (false, null);
                    transition ??= st.PendingDisconnectTransition is { } pendingDisconnect
                        && pendingDisconnect.OwnershipVersion == ownershipVersion
                            ? pendingDisconnect
                            : st.DisconnectResumeCompletion is { IsCompleted: false }
                                ? LifecycleTransitionCoordinator.EnqueueSuccessor(
                                    st, reservation, ownershipVersion)
                                : LifecycleTransitionCoordinator.ReserveActive(
                                    st, reservation, ownershipVersion);

                    if (!ReferenceEquals(st.ActiveLifecycleTransition, transition))
                    {
                        pendingCompletion = transition.Activated.Task;
                    }
                    else if (st.InputSendPending is not null)
                    {
                        pendingCompletion = Task.WhenAny(
                            st.InputSendPending.Completion.Task,
                            transition.Completion.Task);
                    }
                    else
                    {
                        if (transition.DisconnectOwner is not null
                            && ReferenceEquals(st.HijackOwner, transition.DisconnectOwner))
                        {
                            st.HijackOwner = null;
                            st.HijackOwnerExpiresAt = null;
                            ownershipPublication = OwnershipPublicationToken.Released(
                                workerId, st.HijackOwnershipVersion);
                        }
                        if (st.WorkerWs is null
                            || st.HijackOwnershipVersion != ownershipVersion
                            || _hub.State.IsDashboardHijackActive(st)
                            || _hub.State.HasValidRestLease(st))
                        {
                            return (false, null);
                        }
                        worker = st.WorkerWs;
                        transitionReady = true;
                        break;
                    }
                }

                await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
            }
        }
        finally
        {
            if (!transitionReady && transition is not null)
            {
                try
                {
                    if (ownershipPublication is not null)
                    {
                        PublishOwnershipTransition(ownershipPublication);
                    }
                }
                finally
                {
                    CompleteLifecycleTransition(workerId, transition);
                }
            }
        }
        if (ownershipPublication is not null)
        {
            PublishOwnershipTransition(ownershipPublication);
        }
        var resumed = false;
        Exception? error = null;
        Task? sendTask = null;
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
        bounded.CancelAfter(_hub.ResumeSendTimeout);
        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(msg);
            sendTask = worker!.SendTextAsync(encoded, bounded.Token);
            await sendTask.WaitAsync(_hub.ResumeSendTimeout, ct).ConfigureAwait(false);
            resumed = worker is not IAbortableBrowserWs { IsActive: false };
        }
        catch (Exception ex)
        {
            error = ex;
            ObserveEventualFault(sendTask);
        }
        finally
        {
            if (!resumed)
            {
                if (worker is IAbortableBrowserWs abortable)
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
                await ReconcileWorkerDisconnectAsync(workerId, worker!).ConfigureAwait(false);
            }
            CompleteLifecycleTransition(workerId, transition!);
        }
        return resumed ? (true, null) : (false, error);
    }

    private void CompleteLifecycleTransition(
        string workerId,
        PendingLifecycleTransition transition)
    {
        OwnershipPublicationToken? ownershipPublication = null;
        var completeAfterPublication = false;
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is not null)
            {
                if (transition.DisconnectOwner is not null
                    && ReferenceEquals(st.HijackOwner, transition.DisconnectOwner))
                {
                    st.HijackOwner = null;
                    st.HijackOwnerExpiresAt = null;
                    ownershipPublication = OwnershipPublicationToken.Released(
                        workerId, st.HijackOwnershipVersion);
                }
                completeAfterPublication = ownershipPublication is not null;
                if (!completeAfterPublication)
                {
                    LifecycleTransitionCoordinator.Complete(st, transition);
                }
            }
        }
        if (ownershipPublication is not null)
        {
            PublishOwnershipTransition(ownershipPublication);
        }
        if (completeAfterPublication)
        {
            lock (_hub.SharedLock)
            {
                var st = _hub.Registry.Get(workerId);
                if (st is not null)
                {
                    LifecycleTransitionCoordinator.Complete(st, transition);
                }
            }
        }
        transition.Activated.TrySetResult();
        transition.Completion.TrySetResult();
    }

    private void PublishOwnershipTransition(OwnershipPublicationToken publication)
    {
        try
        {
            _hub.State.NotifyHijackChanged(publication);
        }
        catch
        {
            // Ownership mutation and lifecycle fences remain authoritative.
        }
    }

    public async Task BroadcastHijackStateAsync(string workerId, CancellationToken ct = default)
    {
        List<(object Ws, Dictionary<string, object?> Msg)> fanout = new();
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return;
            foreach (var kv in st.Browsers)
            {
                if (_hub.StartupPendingBrowsers.Contains(kv.Key)) continue;
                fanout.Add((kv.Key, _hub.Router.HijackStateMsgFor(workerId, kv.Key)));
            }
        }

        var results = await Task.WhenAll(fanout.Select(async item =>
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(item.Msg);
            var outcome = await SendBrowserBoundedAsync(item.Ws, encoded, ct).ConfigureAwait(false);
            return (item.Ws, Outcome: outcome);
        })).ConfigureAwait(false);
        PruneBrowsers(workerId, results.Where(result => result.Outcome == BrowserSendOutcome.PeerFailed)
            .Select(result => result.Ws));
        if (results.Any(result => result.Outcome == BrowserSendOutcome.CallerCancelled))
        {
            ct.ThrowIfCancellationRequested();
        }
    }

    /// <summary>Fan-out one control payload to every browser on the worker (worker_connected etc.).</summary>
    public async Task BroadcastToBrowsersAsync(
        string workerId, Dictionary<string, object?> msg, CancellationToken ct = default)
    {
        List<object> browsers = new();
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return;
            browsers.AddRange(st.Browsers.Keys.Where(ws => !_hub.StartupPendingBrowsers.Contains(ws)));
        }

        var encoded = ControlChannelCodec.EncodeControlFrame(msg);
        var results = await Task.WhenAll(browsers.Select(async wsObj =>
            (Ws: wsObj, Outcome: await SendBrowserBoundedAsync(wsObj, encoded, ct).ConfigureAwait(false))))
            .ConfigureAwait(false);
        PruneBrowsers(workerId, results.Where(result => result.Outcome == BrowserSendOutcome.PeerFailed)
            .Select(result => result.Ws));
        if (results.Any(result => result.Outcome == BrowserSendOutcome.CallerCancelled))
        {
            ct.ThrowIfCancellationRequested();
        }
    }

    private async Task<BrowserSendOutcome> SendBrowserBoundedAsync(
        object wsObj,
        string payload,
        CancellationToken ct)
    {
        if (wsObj is not IWorkerWs browser) return BrowserSendOutcome.PeerFailed;
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(_hub.BrowserSendTimeout);
        Task? sendTask = null;
        try
        {
            // WaitAsync enforces our deadline even if a broken transport
            // ignores the cancellation token passed into SendTextAsync.
            sendTask = browser.SendTextAsync(payload, timeout.Token);
            await sendTask.WaitAsync(timeout.Token).ConfigureAwait(false);
            return BrowserSendOutcome.Sent;
        }
        catch
        {
            ObserveEventualFault(sendTask);
            return ct.IsCancellationRequested
                ? BrowserSendOutcome.CallerCancelled
                : BrowserSendOutcome.PeerFailed;
        }
    }

    private static void ObserveEventualFault(Task? task)
    {
        if (task is null) return;
        _ = task.ContinueWith(
            static completed => _ = completed.Exception,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
    }

    private void PruneBrowsers(string workerId, IEnumerable<object> dead)
    {
        foreach (var ws in dead)
        {
            if (ws is IAbortableBrowserWs browser) browser.Abort();
            CleanupBrowser(workerId, ws, retainOwnershipVersion: ws is IAbortableBrowserWs);
        }
    }

    /// <summary>Send terminal bytes / control to REST hijack path target worker.</summary>
    public async Task<(bool Ok, string Reason)> SendRestInputAsync(
        string workerId,
        string hijackId,
        string keys,
        CancellationToken ct = default) =>
        await _hub.Lease.SendRestInputAsync(workerId, hijackId, keys, ct).ConfigureAwait(false);

    /// <summary>Send one control frame fenced to the exact REST hijack lease.</summary>
    public async Task<(bool Ok, string Reason, double? LeaseExpiresAt)> SendRestControlAsync(
        string workerId,
        string hijackId,
        IReadOnlyDictionary<string, object?> message,
        CancellationToken ct = default) =>
        await _hub.Lease.SendRestControlAsync(workerId, hijackId, message, ct).ConfigureAwait(false);
}
