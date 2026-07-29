//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Shell;

namespace Provide.Uterm.Server;

/// <summary>
/// A running session's connector, presented to the hub as a worker.
///
/// The reference's <c>HostedSessionRuntime</c> is a worker: it starts the
/// session's connector and then dials <c>/ws/worker/{id}/term</c> on its own
/// server, so from the hub's side a configured session is indistinguishable
/// from one an external worker attached (<c>server/runtime.py: _run</c> and
/// <c>_bridge_session</c>). That is the whole reason
/// <c>POST /worker/{id}/hijack/acquire</c> mints a lease against a session
/// nobody attached by hand — the lease is taken out against a worker that is
/// really there, and the pause it sends is really delivered.
///
/// This link is that same bridge with the socket taken out: the hub holds it as
/// the worker's <see cref="IWorkerWs"/>, so every byte the hub writes to the
/// worker — the pause on acquire, keys under a lease, a step, the resume on
/// release — arrives on exactly the wire format a WebSocket worker would have
/// received, and is answered by the same connector calls the reference's bridge
/// makes. Nothing here fakes a lease: a hub that has no link registered still
/// refuses with <c>no_worker</c>.
///
/// <para>The one thing it is not is a poll loop. The reference's bridge also
/// drains <c>connector.poll_messages()</c> in the background; ushell produces
/// nothing there but the welcome frames (already drained at start-up) and
/// animation frames, so this port answers input synchronously and leaves the
/// loop out rather than run a thread per session for frames that do not come.</para>
/// </summary>
public sealed class LocalWorkerLink : IWorkerWs
{
    private readonly TermHub _hub;
    private readonly string _workerId;
    private readonly UshellConnector _connector;
    private readonly ControlFrameDecoder _decoder = new();

    /// <summary>Serialises decoder + connector, which the hub may reach concurrently.</summary>
    private readonly object _gate = new();

    public LocalWorkerLink(TermHub hub, string workerId, UshellConnector connector)
    {
        _hub = hub;
        _workerId = workerId;
        _connector = connector;
    }

    /// <summary>
    /// Register with the hub and publish what the reference's bridge publishes
    /// the moment it is connected: the session's input mode, then a snapshot
    /// (<c>_bridge_session</c>'s first two sends). The snapshot is what makes
    /// <c>GET /worker/{id}/hijack/{lease}/snapshot</c> answer a real screen
    /// instead of the empty placeholder the route falls back to.
    ///
    /// Returns false when the hub would not take the worker (its
    /// <c>max_workers</c> ceiling), leaving the session running but unattached —
    /// the same refusal a WebSocket worker would have met.
    /// </summary>
    public async Task<bool> AttachAsync(string inputMode, CancellationToken cancellationToken = default)
    {
        if (!_hub.Conn.RegisterWorker(_workerId, this))
        {
            return false;
        }

        var opening = new List<Dictionary<string, object?>>(_connector.SetMode(inputMode)) { _connector.GetSnapshot() };
        await PublishAsync(opening, cancellationToken).ConfigureAwait(false);
        await _hub.Conn.BroadcastToBrowsersAsync(
            _workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "worker_connected",
                ["worker_id"] = _workerId,
                ["ts"] = ShellFrames.NowTs(),
            },
            cancellationToken).ConfigureAwait(false);
        return true;
    }

    /// <summary>Take the worker away again, as a dropped WebSocket would.</summary>
    public void Detach() => _hub.Conn.DeregisterWorker(_workerId, this);

    /// <summary>
    /// The hub writing to the worker. The payload is whatever a WebSocket worker
    /// would have received: control frames from
    /// <c>ConnectionManager.SendWorkerAsync</c>, or raw terminal bytes from
    /// <c>SendRestInputAsync</c> — which is why it is decoded rather than
    /// assumed to be one or the other.
    /// </summary>
    public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
        PublishAsync(Answer(payload), cancellationToken);

    /// <summary>
    /// What the connector answers to one inbound payload. Mirrors the
    /// reference's <c>_process_inbound</c> / <c>_process_control_msg</c>.
    /// </summary>
    private List<Dictionary<string, object?>> Answer(string payload)
    {
        var responses = new List<Dictionary<string, object?>>();
        lock (_gate)
        {
            foreach (var chunk in _decoder.Feed(payload))
            {
                switch (chunk)
                {
                    case DataChunk data:
                        responses.AddRange(_connector.HandleInput(data.Data));
                        break;
                    case ControlChunk control:
                        responses.AddRange(AnswerControl(control.Control));
                        break;
                    default:
                        break;
                }
            }
        }

        return responses;
    }

    /// <summary>One decoded control message, dispatched as the reference dispatches it.</summary>
    private IReadOnlyList<Dictionary<string, object?>> AnswerControl(Dictionary<string, object?> message)
    {
        var type = message.TryGetValue("type", out var t) ? t?.ToString() : null;
        return type switch
        {
            "snapshot_req" => [_connector.GetSnapshot()],
            "analyze_req" =>
            [
                new Dictionary<string, object?>
                {
                    ["type"] = "analysis",
                    ["formatted"] = _connector.GetAnalysis(),
                    ["ts"] = ShellFrames.NowTs(),
                },
            ],
            "control" => _connector.HandleControl(
                message.TryGetValue("action", out var action) ? action?.ToString() ?? "" : ""),
            _ => [],
        };
    }

    /// <summary>
    /// Put the worker's frames where a WebSocket worker's would have gone:
    /// snapshots become the hub's last snapshot, terminal output becomes a
    /// <c>term</c> event, and every frame is fanned out to the browsers —
    /// the same three things <c>HandleWorkerWs</c> does with what it reads.
    /// </summary>
    private async Task PublishAsync(
        IReadOnlyList<Dictionary<string, object?>> frames, CancellationToken cancellationToken)
    {
        foreach (var frame in frames)
        {
            var type = frame.TryGetValue("type", out var t) ? t?.ToString() ?? "term" : "term";
            if (type == "snapshot")
            {
                _hub.Conn.UpdateLastSnapshot(_workerId, frame);
            }
            else if (type == "term")
            {
                _hub.AppendEventData(_workerId, "term", new Dictionary<string, object?>
                {
                    ["data"] = frame.TryGetValue("data", out var data) ? data : "",
                });
                _hub.State.TouchActivity(_workerId);
            }

            await _hub.Conn.BroadcastToBrowsersAsync(_workerId, frame, cancellationToken).ConfigureAwait(false);
        }
    }
}
