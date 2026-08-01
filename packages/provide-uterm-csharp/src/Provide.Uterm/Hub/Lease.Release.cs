//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Hub;

// HijackLeaseManager: ownership restore, lease extend/touch, REST and dashboard release, and REST send paths.
public sealed partial class HijackLeaseManager
{
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

    /// <summary>
    /// Atomically verifies that <paramref name="ws"/> is the current active
    /// dashboard owner and renews that exact lease. Browser control handlers
    /// use this instead of separating an owner check from <see cref="TouchOwner"/>,
    /// which would allow ownership to change between authorization and touch.
    /// </summary>
    public double? TouchIfOwner(string workerId, object ws, int? leaseS = null)
    {
        double? expiration;
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null
                || !_hub.IsDashboardHijackActive(st)
                || !ReferenceEquals(st.HijackOwner, ws))
            {
                return null;
            }

            var ttl = leaseS is null ? _dashboardLeaseS : ClampDashboardLease(leaseS.Value);
            st.HijackOwnerExpiresAt = _clock.Monotonic() + ttl;
            expiration = st.HijackOwnerExpiresAt;
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
        var result = await SendRestPayloadAsync(workerId, hijackId, keys, ct).ConfigureAwait(false);
        return (result.Ok, result.Reason);
    }

    /// <summary>Atomically authorize and deliver a control frame for the exact REST lease.</summary>
    public Task<(bool Ok, string Reason, double? LeaseExpiresAt)> SendRestControlAsync(
        string workerId,
        string hijackId,
        IReadOnlyDictionary<string, object?> message,
        CancellationToken ct = default) =>
        SendRestPayloadAsync(
            workerId,
            hijackId,
            ControlChannelCodec.EncodeControlFrame(message),
            ct);

    private async Task<(bool Ok, string Reason, double? LeaseExpiresAt)> SendRestPayloadAsync(
        string workerId,
        string hijackId,
        string payload,
        CancellationToken ct)
    {
        PendingInputSend? pending = null;
        while (true)
        {
            Task? pendingCompletion = null;
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is null) return (false, "invalid_hijack", null);
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
                    return (false, "invalid_hijack", null);
                }
                else if (st.WorkerWs is null)
                {
                    return (false, "no_worker", null);
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
                        restLeaseExpiresAt: st.HijackSession.LeaseExpiresAt,
                        dashboardOwner: null,
                        dashboardOwnershipVersion: null);
                    st.InputSendPending = pending;
                    break;
                }
            }

            await pendingCompletion!.WaitAsync(ct).ConfigureAwait(false);
        }

        return await DeliverReservedInputAsync(workerId, pending!, payload, ct).ConfigureAwait(false)
            ? (true, "", pending!.RestLeaseExpiresAt)
            : (false, "send_failed", null);
    }
}
