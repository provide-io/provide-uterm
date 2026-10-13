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
    /// How many per-session cutoffs are held at once. When the table is full a
    /// revoke for a session not already in it is refused (the route answers 429)
    /// rather than evicting a cutoff that may still protect another session.
    /// </summary>
    public const int ShareViewerRevocationsMax = 10000;

    /// <summary>
    /// How many cutoffs one revoking principal holds at once, so no single owner
    /// can fill the table. Past it, that principal's own oldest cutoff makes room
    /// for its newest. A global admin is not capped.
    /// </summary>
    public const int ShareViewerRevocationsPerPrincipalMax = 100;

    /// <summary>
    /// How long a cutoff is kept. Share tokens must live well under this: a token
    /// that outlives its cutoff is admitted again.
    /// </summary>
    public const double ShareViewerRevocationTtlSeconds = 86400;

    private readonly object _revocationLock = new();
    private readonly Dictionary<string, double> _revocations = new(StringComparer.Ordinal);
    private readonly LinkedList<string> _revocationOrder = new();

    // Session -> the capped principal that last revoked it, and each such
    // principal's sessions, oldest first.
    private readonly Dictionary<string, string> _revokers = new(StringComparer.Ordinal);
    private readonly Dictionary<string, LinkedList<string>> _revokedBy = new(StringComparer.Ordinal);

    /// <summary>How many cutoffs are held (tests).</summary>
    internal int ShareViewerRevocationCount
    {
        get
        {
            lock (_revocationLock) return _revocations.Count;
        }
    }

    /// <summary>True when <paramref name="p"/> is a read-only share grant for <paramref name="sessionId"/>.</summary>
    public static bool IsShareViewer(Principal p, string sessionId) =>
        p.AdminSessionScope is null
        && p.SubjectId.StartsWith("share:" + sessionId + ":", StringComparison.Ordinal)
        && p.Roles.All(role => role == "viewer");

    /// <summary>
    /// Refuse every share-viewer token for <paramref name="sessionId"/> issued at
    /// or before <paramref name="now"/> (wall-clock seconds, the clock a token's
    /// iat is written in) and return the cutoff. A token issued in the same
    /// second is refused too: iat has one-second resolution. The cutoff counts
    /// against <paramref name="revoker"/>, who holds at most
    /// <see cref="ShareViewerRevocationsPerPrincipalMax"/> of them, its oldest
    /// evicted first; <c>null</c> (a global admin) is not capped.
    /// </summary>
    public double RevokeShareViewers(string sessionId, double now, string? revoker = null)
    {
        lock (_revocationLock)
        {
            // Order is revocation order, so the stale cutoffs are at the front.
            while (_revocationOrder.First is { } oldest && _revocations[oldest.Value] < now - ShareViewerRevocationTtlSeconds)
            {
                DropRevocation(oldest.Value);
            }
            var seen = _revocations.ContainsKey(sessionId);
            DropRevocation(sessionId);
            if (revoker is not null && _revokedBy.TryGetValue(revoker, out var mine)
                && mine.Count >= ShareViewerRevocationsPerPrincipalMax)
            {
                DropRevocation(mine.First!.Value);
            }
            // Nothing was dropped above unless this session or the revoker's own
            // oldest made room, so a refusal leaves the table as it was.
            if (!seen && _revocations.Count >= ShareViewerRevocationsMax)
            {
                throw new ShareViewerRevocationsFullException();
            }
            _revocations[sessionId] = now;
            _revocationOrder.AddLast(sessionId);
            if (revoker is not null)
            {
                _revokers[sessionId] = revoker;
                if (!_revokedBy.TryGetValue(revoker, out var list)) _revokedBy[revoker] = list = new LinkedList<string>();
                list.AddLast(sessionId);
            }
            return now;
        }
    }

    /// <summary>Drop <paramref name="sessionId"/>'s cutoff: the session was deleted.</summary>
    public void ForgetShareViewers(string sessionId)
    {
        lock (_revocationLock)
        {
            DropRevocation(sessionId);
        }
    }

    // The caller holds _revocationLock. Ageing out and eviction take the oldest,
    // so those removals are from the front of each list.
    private void DropRevocation(string sessionId)
    {
        if (!_revocations.Remove(sessionId)) return;
        RemoveFromOldest(_revocationOrder, sessionId);
        if (!_revokers.Remove(sessionId, out var revoker)) return;
        var mine = _revokedBy[revoker];
        RemoveFromOldest(mine, sessionId);
        if (mine.Count == 0) _revokedBy.Remove(revoker);
    }

    private static void RemoveFromOldest(LinkedList<string> list, string id)
    {
        if (list.First is { } first && first.Value == id) list.RemoveFirst();
        else list.Remove(id);
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

/// <summary>The cutoff table is full of live cutoffs; a new session cannot be added.</summary>
public sealed class ShareViewerRevocationsFullException : InvalidOperationException
{
    public ShareViewerRevocationsFullException()
        : base("share-viewer revocation table is full")
    {
    }
}
