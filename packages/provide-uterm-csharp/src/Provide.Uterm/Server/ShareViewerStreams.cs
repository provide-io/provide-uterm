//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Server;

/// <summary>
/// The open event streams of share viewers, so a viewer revoke can end them
/// (Python viewer_revocation.ShareViewerStreams, Go viewer_streams.go).
/// </summary>
internal sealed class ShareViewerStreams
{
    /// <summary>The last SSE payload a share viewer's stream gets before a revoke ends it.</summary>
    internal const string RevokedEvent = "{\"type\":\"viewer_access_revoked\",\"reason\":\"viewer access revoked\"}";

    private readonly object _lock = new();
    private readonly Dictionary<string, Dictionary<CancellationTokenSource, Principal>> _open = new(StringComparer.Ordinal);

    /// <summary>One stream's registration. A stream that is not a share viewer's is never ended.</summary>
    internal sealed class Registration : IDisposable
    {
        private readonly Action _release;

        internal Registration(CancellationToken token, Action release)
        {
            Token = token;
            _release = release;
        }

        /// <summary>Cancelled when a revoke ends the stream.</summary>
        public CancellationToken Token { get; }

        public void Dispose() => _release();
    }

    /// <summary>Register <paramref name="p"/>'s stream on <paramref name="sessionId"/> if it is a share viewer's.</summary>
    public Registration Register(string sessionId, Principal p)
    {
        if (!AuthorizationService.IsShareViewer(p, sessionId))
        {
            return new Registration(CancellationToken.None, static () => { });
        }

        var ended = new CancellationTokenSource();
        lock (_lock)
        {
            if (!_open.TryGetValue(sessionId, out var streams))
            {
                streams = new Dictionary<CancellationTokenSource, Principal>();
                _open[sessionId] = streams;
            }
            streams[ended] = p;
        }

        return new Registration(ended.Token, () =>
        {
            lock (_lock)
            {
                if (_open.TryGetValue(sessionId, out var streams) && streams.Remove(ended) && streams.Count == 0)
                {
                    _open.Remove(sessionId);
                }
            }
            ended.Dispose();
        });
    }

    /// <summary>End every open stream on the session whose principal the cutoff refuses.</summary>
    public int Revoke(AuthorizationService authz, string sessionId)
    {
        lock (_lock)
        {
            if (!_open.TryGetValue(sessionId, out var streams)) return 0;
            var ended = streams.Where(kv => authz.ShareViewerRevoked(kv.Value, sessionId)).Select(kv => kv.Key).ToList();
            foreach (var cts in ended)
            {
                streams.Remove(cts);
                cts.Cancel();
            }
            if (streams.Count == 0) _open.Remove(sessionId);
            return ended.Count;
        }
    }

    /// <summary>How many share-viewer streams are open on the session.</summary>
    public int Count(string sessionId)
    {
        lock (_lock)
        {
            return _open.TryGetValue(sessionId, out var streams) ? streams.Count : 0;
        }
    }
}
