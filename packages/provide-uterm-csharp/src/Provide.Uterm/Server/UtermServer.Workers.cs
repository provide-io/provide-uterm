//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

/// <summary>
/// Bringing a session up and taking it down: its connector, and the worker that
/// connector is presented to the hub as.
///
/// The reference keeps the same two things together in one place
/// (<c>server/runtime.py: HostedSessionRuntime</c>) — start the connector, bridge
/// it to the hub as a worker, and stop both together — because a connector
/// running with no worker attached is a session every client is told about and
/// none can act on.
/// </summary>
public sealed partial class UtermServer
{
    /// <summary>
    /// Live connectors started by profile/quick connect (shell/ushell/pty → ushell).
    /// </summary>
    private readonly ConcurrentDictionary<string, Connectors.IConnector> _liveConnectors =
        new(StringComparer.Ordinal);

    /// <summary>Live worker links for sessions whose connector is bridged to the hub.</summary>
    private readonly ConcurrentDictionary<string, LocalWorkerLink> _workerLinks =
        new(StringComparer.Ordinal);

    /// <summary>
    /// Start session lifecycle, copy connector_config onto status, register hub worker,
    /// and for shell/ushell/pty start a real Ushell connector that pumps term events.
    /// </summary>
    private async Task<SessionStatus?> ActivateSessionAsync(
        string sessionId, SessionDefinition def, CancellationToken cancellationToken = default)
    {
        using var span = Provide.Telemetry.Tracing.GetTracer("provide.uterm.server").StartSpan("ActivateSession");
        span.SetAttribute("session_id", sessionId);
        span.SetAttribute("connector_type", def.ConnectorType);
        
        var st = _deps.Registry.StartSession(sessionId);
        if (st is null) return null;
        st.ConnectorConfig = new Dictionary<string, object?>(def.ConnectorConfig);
        // Ensure hub worker state so ring-buffer events attach to a real worker id.
        // The state this creates carries the session's own input mode: a fresh
        // WorkerTermState says "hijack" (what an unknown worker is assumed to
        // be) and EnrichStatus reads the mode back off the hub, so the default
        // would report every activated session as hijack-only whatever its
        // configuration said. A worker already registered here keeps the mode
        // it reported — the hub learns that from the worker, as the reference does.
        _deps.Hub.Registry.SetDefault(sessionId, new Hub.WorkerTermState { InputMode = def.InputMode });
        var ct = def.ConnectorType.Trim().ToLowerInvariant();
        if (ct is "shell" or "ushell" or "pty")
        {
            // Awaited, not spawned, so REST tests / cover exercise the live path before return.
            await StartShellConnectorAsync(sessionId, def, cancellationToken).ConfigureAwait(false);
        }
        else
        {
            // Non-shell: wire status only + bootstrap event (SSH/telnet need live network).
            _deps.Hub.AppendEventData(sessionId, "session_started", new Dictionary<string, object?>
            {
                ["connector_type"] = def.ConnectorType,
                ["display_name"] = def.DisplayName,
            });
        }

        return st;
    }

    /// <summary>
    /// Start a live ushell connector, pump welcome/term frames onto the EventBus,
    /// and hand the connector to the hub as this session's worker.
    /// Ushell is process-free and deterministic (no PTY / external process).
    ///
    /// The attachment is the part that matters to a client: the reference's
    /// runtime bridges its connector to the hub over <c>/ws/worker/{id}/term</c>,
    /// so a configured session can be leased, snapshotted and typed at through
    /// the hijack routes. Starting the connector without attaching it leaves
    /// every one of those answering "No worker connected for this session."
    /// </summary>
    private async Task StartShellConnectorAsync(
        string sessionId, SessionDefinition def, CancellationToken cancellationToken = default)
    {
        var connector = new Shell.UshellConnector(sessionId, new Shell.UshellConnectorConfig
        {
            DisplayName = def.DisplayName,
            PollDelay = TimeSpan.FromMilliseconds(1),
            PollSleep = static _ => { },
        });
        connector.Start();
        _liveConnectors[sessionId] = connector;
        _deps.Hub.AppendEventData(sessionId, "session_started", new Dictionary<string, object?>
        {
            ["connector_type"] = def.ConnectorType,
            ["display_name"] = def.DisplayName,
            ["shell"] = true,
            ["live"] = true,
        });
        // Welcome path returns immediately (no idle sleep).
        foreach (var frame in connector.PollMessages())
        {
            var et = frame.TryGetValue("type", out var t) ? t?.ToString() ?? "term" : "term";
            if (frame.TryGetValue("data", out var d) && d is Dictionary<string, object?> data)
            {
                _deps.Hub.AppendEventData(sessionId, et, data);
            }
            else
            {
                _deps.Hub.AppendEventData(sessionId, et, frame);
            }
        }

        var link = new LocalWorkerLink(_deps.Hub, sessionId, connector);
        if (await link.AttachAsync(def.InputMode, cancellationToken).ConfigureAwait(false))
        {
            _workerLinks[sessionId] = link;
        }
    }

    /// <summary>
    /// Take a session's connector away: the worker leaves the hub and the
    /// connector stops. Called wherever the session stops, because a stopped
    /// session that still presented a worker could be leased — and would report
    /// itself connected, the hub having a live worker for it.
    /// </summary>
    private void StopLiveConnector(string sessionId)
    {
        if (_workerLinks.TryRemove(sessionId, out var link))
        {
            link.Detach();
        }

        if (_liveConnectors.TryRemove(sessionId, out var connector) && connector is Shell.UshellConnector ushell)
        {
            ushell.Stop();
        }
    }
}
