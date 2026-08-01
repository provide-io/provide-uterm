//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Hub;

// ConnectionManager: force release, browser registration/cleanup, worker sends, broadcasts, and REST send facade.
public sealed partial class ConnectionManager
{
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
