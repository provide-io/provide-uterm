//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.ServerAuth;

/// <summary>
/// Share-viewer revocation (port of authorization.py's revoke_share_viewers).
/// A share viewer is a <c>share:{sessionId}:...</c> subject holding nothing
/// beyond the viewer role and no session-scoped admin grant. A per-session
/// cutoff refuses every such token issued at or before it, checked ahead of the
/// provider so neither a webhook nor a public visibility can readmit one.
/// Process-local, like the tunnel token store.
/// </summary>
public sealed partial class AuthorizationService
{
    /// <summary>
    /// How many per-session cutoffs are held at once. Past it the oldest is
    /// forgotten, which can only matter for a share token that outlived
    /// thousands of later revocations.
    /// </summary>
    public const int ShareViewerRevocationsMax = 10000;

    /// <summary>
    /// How long a cutoff is kept. A cutoff only matters while a token issued
    /// before it can still be valid; ageing out rather than only evicting by
    /// count means nobody can push another session's fresh cutoff out of the table.
    /// </summary>
    public const double ShareViewerRevocationTtlSeconds = 86400;

    private readonly object _revocationLock = new();
    private readonly Dictionary<string, double> _revocations = new(StringComparer.Ordinal);
    private readonly LinkedList<string> _revocationOrder = new();

    /// <summary>True when <paramref name="p"/> is a read-only share grant for <paramref name="sessionId"/>.</summary>
    public static bool IsShareViewer(Principal p, string sessionId) =>
        p.AdminSessionScope is null
        && p.SubjectId.StartsWith("share:" + sessionId + ":", StringComparison.Ordinal)
        && p.Roles.All(role => role == "viewer");

    /// <summary>
    /// Refuse every share-viewer token for <paramref name="sessionId"/> issued at
    /// or before <paramref name="now"/> (wall-clock seconds, the clock a token's
    /// iat is written in) and return the cutoff. A token issued in the same
    /// second is refused too: iat has one-second resolution.
    /// </summary>
    public double RevokeShareViewers(string sessionId, double now)
    {
        lock (_revocationLock)
        {
            if (_revocations.ContainsKey(sessionId)) _revocationOrder.Remove(sessionId);
            _revocations[sessionId] = now;
            _revocationOrder.AddLast(sessionId);
            // Order is revocation order, so the stale cutoffs are at the front.
            while (_revocations[_revocationOrder.First!.Value] < now - ShareViewerRevocationTtlSeconds)
            {
                _revocations.Remove(_revocationOrder.First.Value);
                _revocationOrder.RemoveFirst();
            }
            while (_revocationOrder.Count > ShareViewerRevocationsMax)
            {
                _revocations.Remove(_revocationOrder.First!.Value);
                _revocationOrder.RemoveFirst();
            }
            return now;
        }
    }

    /// <summary>Drop <paramref name="sessionId"/>'s cutoff: the session was deleted.</summary>
    public void ForgetShareViewers(string sessionId)
    {
        lock (_revocationLock)
        {
            if (_revocations.Remove(sessionId)) _revocationOrder.Remove(sessionId);
        }
    }

    /// <summary>The cutoff recorded for <paramref name="sessionId"/>, if its viewers were revoked.</summary>
    public double? ShareViewerRevocation(string sessionId)
    {
        lock (_revocationLock)
        {
            return _revocations.TryGetValue(sessionId, out var cutoff) ? cutoff : null;
        }
    }

    /// <summary>
    /// True when <paramref name="p"/> is a share viewer of <paramref name="sessionId"/>
    /// whose token predates the cutoff. A token without a numeric iat cannot show
    /// it postdates the cutoff, so it is refused.
    /// </summary>
    public bool ShareViewerRevoked(Principal p, string sessionId)
    {
        if (ShareViewerRevocation(sessionId) is not { } cutoff || !IsShareViewer(p, sessionId)) return false;
        double? issued = p.Claims.GetValueOrDefault("iat") switch
        {
            double d => d,
            long l => l,
            int i => i,
            _ => null,
        };
        return issued is not { } iat || iat <= cutoff;
    }
}
