//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Provide.Uterm.Recording;

namespace Provide.Uterm.Server;

/// <summary>
/// One recording per session instance, never shared with a later holder of the
/// id. The reference's hosted runtime starts its recording with a per-runtime
/// instance and its registry retires a deleted session's recording; this port
/// writes recordings straight from the server (operator and detector
/// annotations), so the server does both:
///
/// <list type="bullet">
/// <item>the first write for a session starts its recording with an instance
/// drawn for the session definition, so the store's StartSessionAsync rule
/// applies (a recording another instance left is moved aside);</item>
/// <item>deleting a session ends and retires its recording, and creating one
/// retires anything left under the id; the id stays reserved meanwhile, and a
/// deleted session writes nothing more.</item>
/// </list>
/// </summary>
public sealed partial class UtermServer
{
    /// <summary>
    /// The longest session id, in UTF-8 bytes: <c>&lt;id&gt;.jsonl</c> and its
    /// retired name, 35 bytes longer, both fit a 255-byte filename. The same
    /// limit holds in every port.
    /// </summary>
    internal const int MaxSessionIdBytes = 200;

    // The reference's ^[\w\-]+$ (ASCII here, as in the Go port). Stricter than
    // SafeId, which also admits "." -- and a "." is what keeps a retired
    // recording's name from being any session's recording path.
    private static readonly Regex CreatableSessionId = new(@"^[A-Za-z0-9_-]+$", RegexOptions.Compiled);

    /// <summary>Why <paramref name="id"/> cannot name a session, or null when it can.</summary>
    internal static string? SessionIdError(string id)
    {
        if (!CreatableSessionId.IsMatch(id))
        {
            return "session_id must match ^[\\w\\-]+$";
        }

        return Encoding.UTF8.GetByteCount(id) > MaxSessionIdBytes
            ? $"session_id must be at most {MaxSessionIdBytes} bytes (UTF-8)"
            : null;
    }

    // Serializes every recording write with every retirement, so a write either
    // lands before a session's recording is retired or not at all.
    private readonly SemaphoreSlim _recordingGate = new(1, 1);

    // The instance each session's recording was started under. Guarded by _recordingGate.
    private readonly Dictionary<string, string> _recordingInstances = new(StringComparer.Ordinal);

    private readonly object _retiringLock = new();

    // Ids being deleted, or whose leftover recording is being retired for a new
    // session; each completes when the id is released. Guarded by _retiringLock.
    private readonly Dictionary<string, TaskCompletionSource> _retiringIds = new(StringComparer.Ordinal);

    /// <summary>How long a create waits for a teardown holding its id. Port of _RESERVED_ID_WAIT_S.</summary>
    internal static TimeSpan ReservedIdWait { get; set; } = TimeSpan.FromSeconds(15);

    /// <summary>
    /// How long a removal waits on the recording (the write gate, the final
    /// EndSessionAsync) before it retires and releases regardless. Port of
    /// _REMOVAL_STOP_TIMEOUT_S.
    /// </summary>
    internal static TimeSpan RemovalStopTimeout { get; set; } = TimeSpan.FromSeconds(10);

    private static readonly Provide.Telemetry.Logger RecordingLog =
        Provide.Telemetry.ProvideTelemetry.GetLogger("provide.uterm.server.recording");

    private bool TryReserveId(string sessionId)
    {
        lock (_retiringLock)
        {
            return _retiringIds.TryAdd(sessionId, new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously));
        }
    }

    private void ReleaseId(string sessionId)
    {
        lock (_retiringLock)
        {
            if (_retiringIds.Remove(sessionId, out var released))
            {
                released.TrySetResult();
            }
        }
    }

    private bool IsReserved(string sessionId)
    {
        lock (_retiringLock)
        {
            return _retiringIds.ContainsKey(sessionId);
        }
    }

    /// <summary>
    /// Reserve <paramref name="sessionId"/> for a create, waiting out a teardown
    /// (or another create) that holds it, for up to <see cref="ReservedIdWait"/>.
    /// A tty-named PAM id is reused by the next login on that tty, and the relay
    /// retries an open, so the id is often wanted again while its last holder
    /// is still going. Null on success; otherwise the conflict to answer:
    /// "already exists" when the session exists (before or after the wait),
    /// "being deleted" only when the teardown outlasts the wait.
    /// </summary>
    private async Task<string?> ReserveForCreateAsync(string sessionId)
    {
        var deadline = DateTime.UtcNow + ReservedIdWait;
        while (true)
        {
            Task pending;
            lock (_retiringLock)
            {
                if (_deps.Registry.TryGetDefinition(sessionId, out _))
                {
                    return "session already exists: " + sessionId;
                }

                if (!_retiringIds.TryGetValue(sessionId, out var holder))
                {
                    _retiringIds[sessionId] = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                    return null;
                }

                pending = holder.Task;
            }

            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero
                || await Task.WhenAny(pending, Task.Delay(remaining)).ConfigureAwait(false) != pending)
            {
                return "session is being deleted: " + sessionId;
            }
        }
    }

    /// <summary>
    /// Write <paramref name="events"/> to the session's recording, starting it
    /// first if this is the session's first write. A session that no longer
    /// exists, or is being deleted, writes nothing.
    /// </summary>
    private async Task AppendSessionRecordingAsync(string sessionId, IReadOnlyList<Event> events)
    {
        await _recordingGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (IsReserved(sessionId) || !_deps.Registry.TryGetDefinition(sessionId, out _))
            {
                return;
            }

            bool started;
            lock (_recordingInstances)
            {
                started = _recordingInstances.ContainsKey(sessionId);
            }

            if (!started)
            {
                var instance = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(16));
                await _recording.StartSessionAsync(sessionId, new Dictionary<string, object?>
                {
                    ["started_at"] = _clock.Wall(),
                    [RecordingInstance.Key] = instance,
                }).ConfigureAwait(false);
                lock (_recordingInstances)
                {
                    _recordingInstances[sessionId] = instance;
                }
            }

            await _recording.AppendEventsAsync(sessionId, events).ConfigureAwait(false);
        }
        finally
        {
            _recordingGate.Release();
        }
    }

    /// <summary>
    /// End the session's recording if this server started one, then retire it.
    /// Waiting on the write gate and on the final EndSessionAsync is bounded by
    /// <see cref="RemovalStopTimeout"/>: a write that hangs must not hold the id
    /// forever, so past the bound it is logged and the recording retired anyway.
    /// </summary>
    private async Task RetireSessionRecordingAsync(string sessionId)
    {
        var gated = await _recordingGate.WaitAsync(RemovalStopTimeout).ConfigureAwait(false);
        try
        {
            bool started;
            lock (_recordingInstances)
            {
                started = _recordingInstances.Remove(sessionId);
            }

            if (started)
            {
                var end = _recording.EndSessionAsync(sessionId);
                if (await Task.WhenAny(end, Task.Delay(RemovalStopTimeout)).ConfigureAwait(false) != end)
                {
                    LogStopTimedOut(sessionId, "end_session");
                }
            }

            if (!gated)
            {
                LogStopTimedOut(sessionId, "recording_gate");
            }

            await _recording.RetireSessionAsync(sessionId).ConfigureAwait(false);
        }
        finally
        {
            if (gated)
            {
                _recordingGate.Release();
            }
        }
    }

    private static void LogStopTimedOut(string sessionId, string waitingOn) =>
        RecordingLog.Error("session_removal_stop_timed_out", new Dictionary<string, object?>
        {
            ["session_id"] = sessionId,
            ["waiting_on"] = waitingOn,
            ["timeout_s"] = RemovalStopTimeout.TotalSeconds,
        });

    /// <summary>
    /// Delete a session and retire its recording, holding the id until both
    /// are done. False when another delete of the id is already under way.
    /// </summary>
    private async Task<bool> DeleteSessionAsync(string sessionId)
    {
        if (!TryReserveId(sessionId))
        {
            return false;
        }

        try
        {
            var existed = _deps.Registry.Delete(sessionId);
            await RetireSessionRecordingAsync(sessionId).ConfigureAwait(false);
            return existed;
        }
        finally
        {
            ReleaseId(sessionId);
        }
    }
}
