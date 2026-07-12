//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Embed;

/// <summary>
/// In-process session factory. Hosts (e.g. TWX30) call
/// <c>await hub.CreateSessionAsync(options)</c> — no CLI or loopback HTTP required.
/// </summary>
public sealed class EmbedHub : IEmbedHub
{
    private readonly Dictionary<string, IUtermSession> _sessions = new(StringComparer.Ordinal);
    private readonly object _gate = new();
    private int _seq;

    public IReadOnlyCollection<string> SessionIds
    {
        get
        {
            lock (_gate)
            {
                return _sessions.Keys.ToArray();
            }
        }
    }

    public Task<IUtermSession> CreateSessionAsync(EmbedSessionOptions? options = null, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        options ??= new EmbedSessionOptions();
        var id = string.IsNullOrEmpty(options.SessionId)
            ? "embed-" + Interlocked.Increment(ref _seq).ToString("x", System.Globalization.CultureInfo.InvariantCulture)
            : options.SessionId;

        var session = new UtermSession(id, options);
        lock (_gate)
        {
            if (_sessions.ContainsKey(id))
            {
                throw new InvalidOperationException("session already exists: " + id);
            }

            _sessions[id] = session;
        }

        return Task.FromResult<IUtermSession>(session);
    }

    public IUtermSession? GetSession(string sessionId)
    {
        lock (_gate)
        {
            return _sessions.TryGetValue(sessionId, out var s) ? s : null;
        }
    }

    /// <summary>Remove a disposed session from the registry.</summary>
    public bool RemoveSession(string sessionId)
    {
        lock (_gate)
        {
            return _sessions.Remove(sessionId);
        }
    }
}
