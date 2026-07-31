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

    private readonly TermHub _hub;

    internal ConnectionManager(TermHub hub) => _hub = hub;

    public bool AllowRestAcquireFor(string clientId) => _hub.Limiter.AllowRestAcquire(clientId);

    public bool AllowRestSendFor(string clientId) => _hub.Limiter.AllowRestSend(clientId);

    public bool RegisterWorker(string workerId, IWorkerWs ws)
    {
        lock (_hub.SharedLock)
        {
            if (_hub.Registry.Count >= _hub.MaxWorkers && !_hub.Registry.Contains(workerId))
            {
                return false;
            }

            var st = _hub.State.GetOrCreate(workerId);
            st.WorkerWs = ws;
            st.LastActivityAt = _hub.Clock.Monotonic();
            return true;
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
    public bool SetWorkerHello(string workerId, string mode, int? protocolVersion = null)
    {
        bool blocked;
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return false;

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
            return false;
        }

        return true;
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

    public (bool ShouldBroadcast, bool WasHijacked) DeregisterWorker(string workerId, IWorkerWs ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null || !ReferenceEquals(st.WorkerWs, ws))
            {
                return (false, false);
            }

            // Match Go/Python: drop hijack leases when the worker dies so
            // reconnect does not leave browsers stuck in "Hijacked (you)".
            var wasHijacked = st.HijackSession is not null || st.HijackOwner is not null;
            st.WorkerWs = null;
            st.HijackSession = null;
            st.HijackOwner = null;
            st.HijackOwnerExpiresAt = null;
            st.HijackPending = null;
            return (true, wasHijacked);
        }
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
        var owner = "server-forced";
        var had = false;
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return false;
            if (st.HijackSession is not null)
            {
                owner = st.HijackSession.Owner;
                st.HijackSession = null;
                had = true;
            }

            if (_hub.State.IsDashboardHijackActive(st))
            {
                had = true;
            }

            // Only a live owner is a release worth announcing, but the fields
            // are cleared either way — an owner whose lease already ran out is
            // stale state, not a holder, and leaving it behind would keep
            // answering "hijacked" to anything reading it before the expiry
            // sweep runs.
            st.HijackOwner = null;
            st.HijackOwnerExpiresAt = null;
            // A reserve mid-flight is dropped too: TryAcquireRestAsync only
            // installs its session while its own pending marker is still
            // there, so clearing it makes an acquire racing this release fail
            // rather than land a lease on a session that is being opened.
            st.HijackPending = null;
        }

        if (!had) return false;
        await SendWorkerAsync(workerId, HijackLeaseManager.ResumeFrame(owner, _hub.Clock.Wall()), ct)
            .ConfigureAwait(false);
        _hub.State.NotifyHijackChanged(workerId, false, null);
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
        lock (_hub.SharedLock)
        {
            long? ownershipVersion = _hub.PendingBrowserOwnershipVersions.Remove(ws, out var pendingVersion)
                ? pendingVersion
                : null;
            var st = _hub.Registry.Get(workerId);
            if (st is not null)
            {
                st.Browsers.Remove(ws);
                if (ReferenceEquals(st.HijackOwner, ws))
                {
                    if (_hub.State.IsDashboardHijackActive(st))
                    {
                        ownershipVersion = st.HijackOwnershipVersion;
                    }

                    st.HijackOwner = null;
                    st.HijackOwnerExpiresAt = null;
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
            return ownershipVersion;
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

        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(msg);
            await ws.SendTextAsync(encoded, ct).ConfigureAwait(false);
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
        IWorkerWs worker;
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st?.WorkerWs is null
                || st.HijackOwnershipVersion != ownershipVersion
                || _hub.State.IsDashboardHijackActive(st)
                || _hub.State.HasValidRestLease(st)
                || st.HijackPending is not null)
            {
                return (false, null);
            }

            // Both dashboard and REST acquisition paths reject HijackPending.
            // Hold this reservation until the resume send completes so an
            // owner cannot appear after the generation check but before the
            // worker observes the resume.
            st.HijackPending = reservation;
            worker = st.WorkerWs;
        }

        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(msg);
            await worker.SendTextAsync(encoded, ct).ConfigureAwait(false);
            return (true, null);
        }
        catch (Exception ex)
        {
            return (false, ex);
        }
        finally
        {
            lock (_hub.SharedLock)
            {
                var st = _hub.Registry.Get(workerId);
                if (st?.HijackPending == reservation) st.HijackPending = null;
            }
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
        CancellationToken ct = default)
    {
        if (!_hub.Lease.CheckValid(workerId, hijackId))
        {
            return (false, "invalid_hijack");
        }

        IWorkerWs? ws;
        lock (_hub.SharedLock)
        {
            ws = _hub.Registry.Get(workerId)?.WorkerWs;
        }

        if (ws is null)
        {
            return (false, "no_worker");
        }

        try
        {
            // Raw terminal input (not control-framed).
            await ws.SendTextAsync(keys, ct).ConfigureAwait(false);
            _hub.State.TouchActivity(workerId);
            return (true, "");
        }
        catch
        {
            return (false, "send_failed");
        }
    }
}
