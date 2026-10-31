//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.ServerAuth;

/// <summary>
/// Revoking one share subject of a session (port of authorization.py's
/// revoke_share_viewer_subject): the session's per-session cutoff, for a single
/// <c>share:{sessionId}:...</c> subject, so the session's other share viewers
/// keep watching. Held under the same lock as the per-session cutoffs.
/// </summary>
public sealed partial class AuthorizationService
{
    /// <summary>
    /// How many subject cutoffs one session holds at once: one per share subject
    /// revoked on its own within the last <see cref="ShareViewerRevocationTtlSeconds"/>.
    /// Past it a new subject is refused (the route answers 429) rather than
    /// evicting one, which would readmit that watcher; a revoke of every viewer
    /// of the session frees them, since it subsumes them.
    /// </summary>
    public const int ShareViewerSubjectRevocationsPerSessionMax = 100;

    /// <summary>
    /// How many subject cutoffs are held at once across every session, the
    /// counterpart of <see cref="ShareViewerRevocationsMax"/>: when it is full a
    /// new subject is refused with <see cref="ShareViewerRevocationsFullException"/>
    /// (the route answers 429) rather than evicting a cutoff that may still
    /// protect another session's viewers.
    /// </summary>
    public const int ShareViewerSubjectRevocationsMax = 10000;

    /// <summary>
    /// The largest revoke body read, in bytes: a <see cref="ShareViewerSubjectMaxBytes"/>
    /// subject in its <c>{"subject": ...}</c> envelope with room for whitespace
    /// and escapes. A longer body is refused before it is read in full.
    /// </summary>
    public const int ShareViewerRevokeBodyMaxBytes = 4096;

    /// <summary>
    /// The longest share subject a revoke accepts, in UTF-8 bytes, so a held
    /// subject cutoff costs a bounded amount of memory.
    /// </summary>
    public const int ShareViewerSubjectMaxBytes = 512;

    // (session, subject) -> cutoff, those keys oldest first, and each session's
    // subjects, so neither a session's count nor its deletion needs a scan.
    private readonly Dictionary<(string Session, string Subject), double> _subjectRevocations = new();
    private readonly LinkedList<(string Session, string Subject)> _subjectRevocationOrder = new();
    private readonly Dictionary<string, HashSet<string>> _subjectsBySession = new(StringComparer.Ordinal);

    /// <summary>
    /// True when <paramref name="subject"/> is one a share viewer of
    /// <paramref name="sessionId"/> could hold: the <c>share:{sessionId}:</c>
    /// prefix <see cref="IsShareViewer"/> ties to the session, something after
    /// it, and no more than <see cref="ShareViewerSubjectMaxBytes"/>.
    /// </summary>
    public static bool IsShareSubjectOf(string subject, string sessionId)
    {
        var prefix = "share:" + sessionId + ":";
        return subject.StartsWith(prefix, StringComparison.Ordinal)
            && subject.Length > prefix.Length
            && Encoding.UTF8.GetByteCount(subject) <= ShareViewerSubjectMaxBytes;
    }

    /// <summary>
    /// Refuse <paramref name="subject"/>'s share-viewer tokens for
    /// <paramref name="sessionId"/> issued at or before <paramref name="now"/> and
    /// return the cutoff: the same clock and same-second rule as
    /// <see cref="RevokeShareViewers"/>, for one subject. A token minted for the
    /// subject after the cutoff is admitted, so that watcher can be let back in.
    /// Throws <see cref="ShareViewerSubjectRevocationsFullException"/> when the
    /// session already holds <see cref="ShareViewerSubjectRevocationsPerSessionMax"/>
    /// subject cutoffs and none is this subject's, and
    /// <see cref="ShareViewerRevocationsFullException"/> when every session
    /// together holds <see cref="ShareViewerSubjectRevocationsMax"/> and this is a
    /// new one. The caller checks <see cref="IsShareSubjectOf"/>.
    /// </summary>
    public double RevokeShareViewerSubject(string sessionId, string subject, double now)
    {
        lock (_revocationLock)
        {
            // Order is revocation order, so the stale cutoffs are at the front.
            while (_subjectRevocationOrder.First is { } oldest
                && _subjectRevocations[oldest.Value] < now - ShareViewerRevocationTtlSeconds)
            {
                DropSubjectRevocation(oldest.Value.Session, oldest.Value.Subject);
            }
            var held = _subjectsBySession.GetValueOrDefault(sessionId);
            var seen = held is not null && held.Contains(subject);
            if (!seen && held is not null && held.Count >= ShareViewerSubjectRevocationsPerSessionMax)
            {
                throw new ShareViewerSubjectRevocationsFullException();
            }
            if (!seen && _subjectRevocations.Count >= ShareViewerSubjectRevocationsMax)
            {
                throw new ShareViewerRevocationsFullException();
            }
            // Dropped and re-added, so a re-revoke moves to the back of the order
            // and ages out from its newest cutoff.
            DropSubjectRevocation(sessionId, subject);
            _subjectRevocations[(sessionId, subject)] = now;
            _subjectRevocationOrder.AddLast((sessionId, subject));
            if (!_subjectsBySession.TryGetValue(sessionId, out var subjects))
            {
                _subjectsBySession[sessionId] = subjects = new HashSet<string>(StringComparer.Ordinal);
            }
            subjects.Add(subject);
            return now;
        }
    }

    // The caller holds _revocationLock. The session's subject cutoffs at or
    // before a session-wide cutoff just recorded refuse nothing it does not (the
    // later cutoff decides), so they go and free their slots.
    private void DropSubsumedSubjectRevocations(string sessionId, double cutoff)
    {
        if (!_subjectsBySession.TryGetValue(sessionId, out var subjects)) return;
        foreach (var subject in subjects.ToList())
        {
            if (_subjectRevocations[(sessionId, subject)] <= cutoff) DropSubjectRevocation(sessionId, subject);
        }
    }

    // The caller holds _revocationLock. Ageing out takes the oldest, so that
    // removal is from the front of the order.
    private void DropSubjectRevocation(string sessionId, string subject)
    {
        if (!_subjectRevocations.Remove((sessionId, subject))) return;
        RemoveFromOldest(_subjectRevocationOrder, (sessionId, subject));
        var subjects = _subjectsBySession[sessionId];
        subjects.Remove(subject);
        if (subjects.Count == 0) _subjectsBySession.Remove(sessionId);
    }

    /// <summary>The cutoff recorded for one share subject of <paramref name="sessionId"/>, if it was revoked.</summary>
    public double? ShareViewerSubjectRevocation(string sessionId, string subject)
    {
        lock (_revocationLock)
        {
            return _subjectRevocations.TryGetValue((sessionId, subject), out var cutoff) ? cutoff : null;
        }
    }
}

/// <summary>The session holds its cap of subject cutoffs; a new subject cannot be added.</summary>
public sealed class ShareViewerSubjectRevocationsFullException : InvalidOperationException
{
    public ShareViewerSubjectRevocationsFullException()
        : base("share-viewer subject revocation table is full for this session")
    {
    }
}
