//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Read-only browser presence queries and worker-bound control frames.</summary>
public sealed class PresenceManager
{
    private readonly TermHub _hub;

    internal PresenceManager(TermHub hub) => _hub = hub;

    public Dictionary<string, object?> RegisterBrowserStateSnapshot(string workerId, object ws)
    {
        lock (_hub.SharedLock)
        {
            var st = _hub.Registry.Get(workerId);
            if (st is null)
            {
                return new Dictionary<string, object?>
                {
                    ["is_hijacked"] = false,
                    ["hijacked_by_me"] = false,
                    ["worker_online"] = false,
                    ["input_mode"] = InputModes.Hijack,
                };
            }

            return new Dictionary<string, object?>
            {
                ["is_hijacked"] = _hub.State.IsHijacked(st),
                ["hijacked_by_me"] = _hub.State.IsDashboardHijackActive(st) && ReferenceEquals(st.HijackOwner, ws),
                ["worker_online"] = st.WorkerWs is not null,
                ["input_mode"] = st.InputMode,
            };
        }
    }

    public bool CanSendInput(WorkerTermState st, object ws)
    {
        if (st.InputMode == InputModes.Open)
        {
            var role = st.Browsers.TryGetValue(ws, out var r) ? r : "viewer";
            return role is "operator" or "admin";
        }

        return _hub.State.IsDashboardHijackActive(st) && ReferenceEquals(st.HijackOwner, ws);
    }

    public async Task RequestSnapshotAsync(string workerId, CancellationToken ct = default)
    {
        await _hub.SendWorkerAsync(workerId, new Dictionary<string, object?>
        {
            ["type"] = "snapshot_req",
            ["req_id"] = Guid.NewGuid().ToString(),
            ["ts"] = _hub.Clock.Wall(),
        }, ct).ConfigureAwait(false);
    }

    public async Task RequestAnalysisAsync(string workerId, CancellationToken ct = default)
    {
        await _hub.SendWorkerAsync(workerId, new Dictionary<string, object?>
        {
            ["type"] = "analyze_req",
            ["req_id"] = Guid.NewGuid().ToString(),
            ["ts"] = _hub.Clock.Wall(),
        }, ct).ConfigureAwait(false);
    }
}
