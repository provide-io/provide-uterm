//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Outbound-frame plumbing: events ring buffer, broadcasts, mode changes.</summary>
public sealed class MessageRouter
{
    private readonly TermHub _hub;
    private readonly int _eventDequeMaxlen;

    internal MessageRouter(TermHub hub, int eventDequeMaxlen)
    {
        _hub = hub;
        _eventDequeMaxlen = Math.Max(1, eventDequeMaxlen);
    }

    public Dictionary<string, object?> AppendEvent(string workerId, string eventType, Dictionary<string, object?>? data)
    {
        var payload = data is null ? new Dictionary<string, object?>() : new Dictionary<string, object?>(data);
        if (eventType == "term" && payload.TryGetValue("data", out var rawObj) && rawObj is string raw)
        {
            var cap = _hub.MaxEventDataChars;
            if (raw.Length > cap)
            {
                payload = new Dictionary<string, object?>(payload) { ["data"] = raw[..cap] };
            }
        }

        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            Dictionary<string, object?> evt;
            if (st is null)
            {
                // Still fan-out so REST watch/SSE work for sessions not yet WS-attached.
                evt = new Dictionary<string, object?>
                {
                    ["seq"] = 0,
                    ["ts"] = _hub.Clock.Wall(),
                    ["type"] = eventType,
                    ["data"] = payload,
                };
            }
            else
            {
                st.EventSeq++;
                evt = new Dictionary<string, object?>
                {
                    ["seq"] = st.EventSeq,
                    ["ts"] = _hub.Clock.Wall(),
                    ["type"] = eventType,
                    ["data"] = payload,
                };
                st.Events.Add(evt);
                if (st.Events.Count > _eventDequeMaxlen)
                {
                    st.Events.RemoveRange(0, st.Events.Count - _eventDequeMaxlen);
                }

                if (st.Events.Count > 0 && st.Events[0].TryGetValue("seq", out var minSeq) && minSeq is int minI)
                {
                    st.MinEventSeq = minI;
                }
            }

            // Fan-out to live watchers (EventBus long-poll / SSE).
            _hub.EventBus.Enqueue(workerId, new Dictionary<string, object?>(evt));
            return evt;
        }
    }

    public void PruneIfIdle(string workerId)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return;
            if (st.WorkerWs is null && st.Browsers.Count == 0 && st.HijackOwner is null && st.HijackSession is null)
            {
                _hub.Registry.Pop(workerId);
            }
        }
    }

    public Dictionary<string, object?> HijackStateMsgFor(string workerId, object? ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null)
            {
                return MakeHijackState(false, null, null, InputModes.Hijack);
            }

            var isDashboard = _hub.State.IsDashboardHijackActive(st);
            var isRest = _hub.State.HasValidRestLease(st);
            var isH = isDashboard || isRest;
            double? leaseExpiresAt = null;
            if (isRest && st.HijackSession is not null)
            {
                leaseExpiresAt = st.HijackSession.LeaseExpiresAt;
            }
            else
            {
                leaseExpiresAt = st.HijackOwnerExpiresAt;
            }

            string? owner = null;
            if (isDashboard && ws is not null && ReferenceEquals(st.HijackOwner, ws))
            {
                owner = "me";
            }
            else if (isDashboard || isRest)
            {
                owner = "other";
            }

            return MakeHijackState(isH, owner, leaseExpiresAt, st.InputMode);
        }
    }

    public List<Dictionary<string, object?>> GetRecentEvents(string workerId, int limit, int afterSeq = 0)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return new List<Dictionary<string, object?>>();
            var lim = limit <= 0 ? 200 : limit;
            return st.Events
                .Where(e => e.TryGetValue("seq", out var s) && s is int si && si > afterSeq)
                .TakeLast(lim)
                .Select(e => new Dictionary<string, object?>(e))
                .ToList();
        }
    }

    /// <summary>
    /// Move a worker between <c>hijack</c> and <c>open</c>, refusing the one
    /// switch that would end a lease's exclusivity behind its holder's back.
    ///
    /// <c>open</c> means everyone connected may type. Flipping a leased
    /// session to it would leave the holder with a lease that still verifies
    /// and still extends — every signal they have says they alone are driving
    /// a terminal anyone can now type into. So the reference refuses that one
    /// transition (<c>bridge/hub/router_impl.py:326-334</c>, Go
    /// <c>hub/router.go:162</c>), and refuses it here rather than in the route
    /// handler: this is the only place the field is written, so no caller can
    /// reach the write with the guard behind them.
    ///
    /// Refused is the requested mode, not the current one — <c>hijack</c>
    /// under a lease is what the mode already is, and callers that re-assert
    /// it before an acquire are answered normally. "Hijacked" is the hub's own
    /// <see cref="StateStore.IsHijacked"/>: a live dashboard owner or a REST
    /// lease that has not expired, so a lease that ran out stops blocking.
    ///
    /// The session route (<c>POST /api/sessions/{id}/mode</c>) is the other
    /// authority over this field and answers differently on purpose — an
    /// operator opening a session is entitled to take it back, so that path
    /// force-releases the lease first and arrives here with nothing held.
    /// </summary>
    public (bool Ok, string Reason) SetInputMode(string workerId, string mode)
    {
        if (mode is not (InputModes.Hijack or InputModes.Open))
        {
            return (false, "invalid_mode");
        }

        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null) return (false, "no_worker");
            if (mode == InputModes.Open && _hub.State.IsHijacked(st))
            {
                return (false, "active_hijack");
            }

            st.InputMode = mode;
            // Every caller of this is an authenticated route — the session
            // routes and the worker-control route, which requires
            // session.control.mode. Reaching here therefore means somebody
            // decided the mode, and a later worker_hello may raise it but never
            // lower it back. See WorkerTermState.InputModeSetByOperator.
            st.InputModeSetByOperator = true;
            return (true, "");
        }
    }

    public Dictionary<string, object?>? GetLastSnapshot(string workerId)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            return st?.LastSnapshot is null ? null : new Dictionary<string, object?>(st.LastSnapshot);
        }
    }

    // Wire field is "hijacked" (Python/Go/schema + frontend validateHijackStateFrame).
    // Not "is_hijacked" — that name is only used on internal hub state dicts.
    private static Dictionary<string, object?> MakeHijackState(bool isHijacked, string? owner, double? leaseExpiresAt, string inputMode) =>
        new()
        {
            ["type"] = "hijack_state",
            ["hijacked"] = isHijacked,
            ["owner"] = owner,
            ["lease_expires_at"] = leaseExpiresAt,
            ["input_mode"] = inputMode,
        };
}
