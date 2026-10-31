//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"errors"
	"strings"
)

// ShareViewerSubjectRevocationsPerSessionMax bounds the subject cutoffs one
// session holds at once (port of SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX):
// one per share subject revoked on its own within the last
// ShareViewerRevocationTTLSeconds. Past it a new subject is refused (the route
// answers 429) rather than evicting one, which would readmit that watcher; a
// revoke of every viewer of the session frees them, since it subsumes them.
const ShareViewerSubjectRevocationsPerSessionMax = 100

// ShareViewerSubjectRevocationsMax bounds the subject cutoffs held at once
// across every session (port of SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX), the
// counterpart of ShareViewerRevocationsMax: when it is full a new subject is
// refused with ErrShareViewerRevocationsFull (the route answers 429) rather
// than evicting a cutoff that may still protect another session's viewers.
const ShareViewerSubjectRevocationsMax = 10000

// ShareViewerRevokeBodyMaxBytes is the largest revoke body read (port of
// SHARE_VIEWER_REVOKE_BODY_MAX_BYTES): a ShareViewerSubjectMaxBytes subject in
// its {"subject": ...} envelope with room for whitespace and escapes. A
// longer body is refused before it is read in full.
const ShareViewerRevokeBodyMaxBytes = 4096

// ShareViewerSubjectMaxBytes is the longest share subject a revoke accepts, in
// UTF-8 bytes (port of SHARE_VIEWER_SUBJECT_MAX_BYTES), so a held subject
// cutoff costs a bounded amount of memory.
const ShareViewerSubjectMaxBytes = 512

// ErrShareViewerSubjectRevocationsFull is returned by RevokeShareViewerSubject
// when the session holds its cap of subject cutoffs and none is this subject's.
var ErrShareViewerSubjectRevocationsFull = errors.New("share-viewer subject revocation table is full for this session")

// subjectKey names one share subject of one session.
type subjectKey struct {
	session string
	subject string
}

// IsShareSubjectOf reports whether subject is one a share viewer of sessionID
// could hold: the "share:{sessionID}:" prefix that IsShareViewer ties to the
// session, something after it, and no more than ShareViewerSubjectMaxBytes.
func IsShareSubjectOf(subject, sessionID string) bool {
	prefix := "share:" + sessionID + ":"
	// len is the UTF-8 length: the route has already refused invalid UTF-8 and
	// lone surrogates, so the subject is real text.
	return strings.HasPrefix(subject, prefix) && subject != prefix && len(subject) <= ShareViewerSubjectMaxBytes
}

// RevokeShareViewerSubject refuses subject's share-viewer tokens for sessionID
// issued at or before now and returns the cutoff (port of
// revoke_share_viewer_subject). The same clock and same-second rule as
// RevokeShareViewers, for one share subject: the session's other viewers are
// untouched, and a token minted for subject after the cutoff is admitted, so
// that watcher can be let back in. It returns
// ErrShareViewerSubjectRevocationsFull when the session holds its cap and
// ErrShareViewerRevocationsFull when every session together holds
// ShareViewerSubjectRevocationsMax, for a new subject only. The caller checks
// IsShareSubjectOf.
func (s *AuthorizationService) RevokeShareViewerSubject(sessionID, subject string, now float64) (float64, error) {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.subjectCutoffs == nil {
		r.subjectCutoffs = map[subjectKey]float64{}
		r.subjectsBySession = map[string]map[string]struct{}{}
	}
	// Order is revocation order, so the stale cutoffs are at the front.
	for len(r.subjectOrder) > 0 && r.subjectCutoffs[r.subjectOrder[0]] < now-ShareViewerRevocationTTLSeconds {
		r.dropSubjectLocked(r.subjectOrder[0])
	}
	key := subjectKey{sessionID, subject}
	held := r.subjectsBySession[sessionID]
	_, seen := held[subject]
	if !seen && len(held) >= ShareViewerSubjectRevocationsPerSessionMax {
		return 0, ErrShareViewerSubjectRevocationsFull
	}
	if !seen && len(r.subjectOrder) >= ShareViewerSubjectRevocationsMax {
		return 0, ErrShareViewerRevocationsFull
	}
	// Dropped and re-added, so a re-revoke moves to the back of the order and
	// ages out from its newest cutoff.
	r.dropSubjectLocked(key)
	r.subjectCutoffs[key] = now
	r.subjectOrder = append(r.subjectOrder, key)
	if r.subjectsBySession[sessionID] == nil {
		r.subjectsBySession[sessionID] = map[string]struct{}{}
	}
	r.subjectsBySession[sessionID][subject] = struct{}{}
	return now, nil
}

// dropSubsumedSubjectsLocked removes sessionID's subject cutoffs at or before
// cutoff, a session-wide cutoff just recorded: they refuse nothing it does not
// (the later cutoff decides), so they go and free their slots. The caller
// holds mu.
func (r *shareViewerRevocations) dropSubsumedSubjectsLocked(sessionID string, cutoff float64) {
	for subject := range r.subjectsBySession[sessionID] {
		if key := (subjectKey{sessionID, subject}); r.subjectCutoffs[key] <= cutoff {
			r.dropSubjectLocked(key)
		}
	}
}

// dropSubjectLocked removes key's cutoff, if any. The caller holds mu.
func (r *shareViewerRevocations) dropSubjectLocked(key subjectKey) {
	if _, seen := r.subjectCutoffs[key]; !seen {
		return
	}
	delete(r.subjectCutoffs, key)
	r.subjectOrder = withoutID(r.subjectOrder, key)
	held := r.subjectsBySession[key.session]
	delete(held, key.subject)
	if len(held) == 0 {
		delete(r.subjectsBySession, key.session)
	}
}

// ShareViewerSubjectRevocation returns the cutoff recorded for one share
// subject of sessionID, if any.
func (s *AuthorizationService) ShareViewerSubjectRevocation(sessionID, subject string) (float64, bool) {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	cutoff, ok := r.subjectCutoffs[subjectKey{sessionID, subject}]
	return cutoff, ok
}
