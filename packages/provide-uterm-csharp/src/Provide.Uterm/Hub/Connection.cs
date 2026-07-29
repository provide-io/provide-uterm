//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Hub;

/// <summary>Worker/browser registration and REST rate-limit facade.</summary>
public sealed class ConnectionManager
{
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

    public Dictionary<string, object?> RegisterBrowser(string workerId, object ws, string role, bool deferBroadcast = false)
    {
        _ = deferBroadcast;
        lock (_hub.SharedLock)
        {
            var st = _hub.State.GetOrCreate(workerId);
            st.Browsers[ws] = string.IsNullOrWhiteSpace(role) ? "viewer" : role;
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
    }

    public void CleanupBrowser(string workerId, object ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return;
            st.Browsers.Remove(ws);
            if (ReferenceEquals(st.HijackOwner, ws))
            {
                st.HijackOwner = null;
                st.HijackOwnerExpiresAt = null;
            }
        }
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

    public async Task BroadcastHijackStateAsync(string workerId, CancellationToken ct = default)
    {
        List<(object Ws, Dictionary<string, object?> Msg)> fanout = new();
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return;
            foreach (var kv in st.Browsers)
            {
                fanout.Add((kv.Key, _hub.Router.HijackStateMsgFor(workerId, kv.Key)));
            }
        }

        foreach (var (wsObj, msg) in fanout)
        {
            if (wsObj is not IWorkerWs browser) continue;
            try
            {
                var encoded = ControlChannelCodec.EncodeControlFrame(msg);
                await browser.SendTextAsync(encoded, ct).ConfigureAwait(false);
            }
            catch
            {
                // drop failed sockets; cleanup happens on next receive error
            }
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
            browsers.AddRange(st.Browsers.Keys);
        }

        var encoded = ControlChannelCodec.EncodeControlFrame(msg);
        foreach (var wsObj in browsers)
        {
            if (wsObj is not IWorkerWs browser) continue;
            try
            {
                await browser.SendTextAsync(encoded, ct).ConfigureAwait(false);
            }
            catch
            {
                // drop failed sockets
            }
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
