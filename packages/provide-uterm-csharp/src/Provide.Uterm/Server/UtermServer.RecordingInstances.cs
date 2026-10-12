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
    // session. Guarded by _retiringLock.
    private readonly HashSet<string> _retiringIds = new(StringComparer.Ordinal);

    private bool TryReserveId(string sessionId)
    {
        lock (_retiringLock)
        {
            return _retiringIds.Add(sessionId);
        }
    }

    private void ReleaseId(string sessionId)
    {
        lock (_retiringLock)
        {
            _retiringIds.Remove(sessionId);
        }
    }

    private bool IsReserved(string sessionId)
    {
        lock (_retiringLock)
        {
            return _retiringIds.Contains(sessionId);
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

            if (!_recordingInstances.ContainsKey(sessionId))
            {
                var instance = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(16));
                await _recording.StartSessionAsync(sessionId, new Dictionary<string, object?>
                {
                    ["started_at"] = _clock.Wall(),
                    [RecordingInstance.Key] = instance,
                }).ConfigureAwait(false);
                _recordingInstances[sessionId] = instance;
            }

            await _recording.AppendEventsAsync(sessionId, events).ConfigureAwait(false);
        }
        finally
        {
            _recordingGate.Release();
        }
    }

    /// <summary>End the session's recording if this server started one, then retire it.</summary>
    private async Task RetireSessionRecordingAsync(string sessionId)
    {
        await _recordingGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_recordingInstances.Remove(sessionId))
            {
                await _recording.EndSessionAsync(sessionId).ConfigureAwait(false);
            }

            await _recording.RetireSessionAsync(sessionId).ConfigureAwait(false);
        }
        finally
        {
            _recordingGate.Release();
        }
    }

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
