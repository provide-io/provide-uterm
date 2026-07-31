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
    Task<(bool Ok, Exception? Error)> SendWorkerAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default);
    Task BroadcastHijackStateAsync(string workerId, CancellationToken ct = default);
    Task AppendEventAsync(string workerId, string eventType, CancellationToken ct = default);
    Task PruneIfIdleAsync(string workerId, CancellationToken ct = default);
}

/// <summary>Multi-worker hijack lease state machine.</summary>
public sealed class HijackLeaseManager
{
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

            if (_hub.IsDashboardHijackActive(st) || _hub.HasValidRestLease(st) || st.HijackPending is not null)
            {
                return (false, "already_hijacked");
            }

            workerWs = st.WorkerWs;
            st.HijackPending = hijackId;
        }

        try
        {
            var encoded = ControlChannelCodec.EncodeControlFrame(PauseFrame(owner, hijackId, _clock.Wall()));
            try
            {
                await workerWs.SendTextAsync(encoded, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch
            {
                lock (_lock)
                {
                    var st = _registry.Get(workerId);
                    if (st is not null && ReferenceEquals(st.WorkerWs, workerWs))
                    {
                        st.WorkerWs = null;
                    }
                }

                return (false, "no_worker");
            }

            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is null || st.HijackPending != hijackId)
                {
                    return (false, "no_worker");
                }

                st.HijackSession = new HijackSession
                {
                    HijackId = hijackId,
                    Owner = owner,
                    AcquiredAt = now,
                    LeaseExpiresAt = now + leaseS,
                    LastHeartbeat = now,
                };
                st.HijackOwnershipVersion++;
                st.HijackPending = null;
            }

            return (true, "");
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null && st.HijackPending == hijackId)
                {
                    st.HijackPending = null;
                }
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
            if (_hub.IsDashboardHijackActive(st) || _hub.HasValidRestLease(st) || st.HijackPending is not null)
            {
                return (false, "already_hijacked");
            }

            st.HijackOwner = ws;
            st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            st.HijackOwnershipVersion++;
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
                    || st.HijackPending is not null)
                {
                    return (false, "already_hijacked");
                }
                else
                {
                    workerWs = st.WorkerWs;
                    st.HijackPending = reservation;
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
            try
            {
                await workerWs.SendTextAsync(encoded, ct).ConfigureAwait(false);
                pauseLanded = true;
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch
            {
                return (false, "no_worker");
            }

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
                    ClearDashboardReservation(st, reservation);
                    return (true, "");
                }
            }

            await CompensateCanceledPauseAsync(workerId, reservation, workerWs)
                .ConfigureAwait(false);
            return (false, "inactive_browser");
        }
        finally
        {
            lock (_lock)
            {
                var st = _registry.Get(workerId);
                if (st is not null) ClearDashboardReservation(st, reservation);
            }
        }

        async Task CompensateCanceledPauseAsync(
            string id,
            string canceledReservation,
            IWorkerWs pausedWorker)
        {
            if (!pauseLanded) return;
            var resumeReservation = "dashboard-resume-" + Guid.NewGuid().ToString("N");
            lock (_lock)
            {
                var st = _registry.Get(id);
                if (st is null) return;
                ClearDashboardReservation(st, canceledReservation);
                if (!ReferenceEquals(st.WorkerWs, pausedWorker)
                    || _hub.IsDashboardHijackActive(st)
                    || _hub.HasValidRestLease(st)
                    || st.HijackPending is not null
                    || st.DisconnectResumeCompletion is { IsCompleted: false })
                {
                    return;
                }

                st.HijackPending = resumeReservation;
            }

            try
            {
                var encodedResume = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                {
                    ["type"] = "control",
                    ["action"] = "resume",
                    ["source"] = "dashboard",
                    ["ts"] = _clock.Wall(),
                });
                // State repair must outlive the browser request that was pruned
                // while its worker send was in flight.
                await pausedWorker.SendTextAsync(encodedResume, CancellationToken.None).ConfigureAwait(false);
            }
            catch
            {
                // The ownership transaction is still canceled; worker failure
                // is reported by the caller as an unsuccessful acquisition.
            }
            finally
            {
                lock (_lock)
                {
                    var st = _registry.Get(id);
                    if (st?.HijackPending == resumeReservation) st.HijackPending = null;
                }
            }
        }
    }

    private static void ClearDashboardReservation(WorkerTermState st, string reservation)
    {
        if (st.HijackPending != reservation) return;
        st.HijackPending = null;
        st.PendingDashboardBrowser = null;
        st.PendingDashboardOwnershipVersion = null;
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
                || st.HijackPending is not null)
            {
                return false;
            }

            st.HijackOwner = ws;
            st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            return true;
        }
    }

    public double? ExtendLease(string workerId, string hijackId, string owner, int leaseS, double now)
    {
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
            return st.HijackSession.LeaseExpiresAt;
        }
    }

    public (bool Released, bool ShouldResume) ReleaseRest(string workerId, string hijackId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st?.HijackSession is null || st.HijackSession.HijackId != hijackId)
            {
                return (false, false);
            }

            st.HijackSession = null;
            var shouldResume = !_hub.IsDashboardHijackActive(st);
            return (true, shouldResume);
        }
    }

    public double? TouchOwner(string workerId, int? leaseS = null)
    {
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
            return exp;
        }
    }

    public (bool Released, bool RestActive) TryReleaseWs(string workerId, object ws)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null || !_hub.IsDashboardHijackActive(st) || !ReferenceEquals(st.HijackOwner, ws))
            {
                var restActive = st is not null && _hub.HasValidRestLease(st);
                return (false, restActive);
            }

            st.HijackOwner = null;
            st.HijackOwnerExpiresAt = null;
            return (true, _hub.HasValidRestLease(st));
        }
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

    public bool PrepareBrowserInput(string workerId, object ws)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null
                || !st.Browsers.ContainsKey(ws)
                || ws is IAbortableBrowserWs { IsActive: false }) return false;
            var allowed = _hub.CanSendInput(st, ws);
            if (_hub.IsDashboardHijackActive(st) && ReferenceEquals(st.HijackOwner, ws))
            {
                st.HijackOwnerExpiresAt = _clock.Monotonic() + _dashboardLeaseS;
            }

            return allowed;
        }
    }

    public (bool BrowserExpired, bool RestExpired) CleanupExpired(string workerId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null) return (false, false);
            var now = _clock.Monotonic();
            var lease = st.Lease();
            var (rest, dash) = lease.Expire(now);
            if (rest || dash)
            {
                st.ApplyLease(lease);
            }

            return (dash, rest);
        }
    }
}
