//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
)

// ShareViewerRevocationsMax bounds how many per-session revocation cutoffs are
// held at once (port of SHARE_VIEWER_REVOCATIONS_MAX). When the table is full a
// revoke for a session not already in it is refused (the route answers 429)
// rather than evicting a cutoff that may still protect another session.
const ShareViewerRevocationsMax = 10000

// ShareViewerRevocationsPerPrincipalMax bounds the cutoffs one revoking
// principal holds at once (port of SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX),
// so no single owner can fill the table. Past it, that principal's own oldest
// cutoff makes room for its newest. A global admin is not capped.
const ShareViewerRevocationsPerPrincipalMax = 100

// ShareViewerRevocationTTLSeconds is how long a cutoff is kept (port of
// SHARE_VIEWER_REVOCATION_TTL_S). Share tokens must live well under this: a
// token that outlives its cutoff is admitted again.
const ShareViewerRevocationTTLSeconds = 86400.0

// ErrShareViewerRevocationsFull is returned by RevokeShareViewers when the
// cutoff table is full of live cutoffs and this session holds none yet.
var ErrShareViewerRevocationsFull = errors.New("share-viewer revocation table is full")

// shareViewerRevocations is the per-session cutoff table behind
// AuthorizationService. The zero value is ready to use.
type shareViewerRevocations struct {
	mu      sync.Mutex
	cutoffs map[string]float64
	order   []string // oldest first
	// revokers maps a session to the capped principal that last revoked it;
	// byRevoker lists each such principal's sessions, oldest first.
	revokers  map[string]string
	byRevoker map[string][]string
}

// withoutID removes id from an oldest-first list. Ageing out and eviction
// both take the oldest, so that case is a re-slice rather than a scan.
func withoutID(ids []string, id string) []string {
	if len(ids) > 0 && ids[0] == id {
		return ids[1:]
	}
	return slices.DeleteFunc(ids, func(other string) bool { return other == id })
}

// dropLocked removes sessionID's cutoff, if any. The caller holds mu.
func (r *shareViewerRevocations) dropLocked(sessionID string) {
	if _, seen := r.cutoffs[sessionID]; !seen {
		return
	}
	delete(r.cutoffs, sessionID)
	r.order = withoutID(r.order, sessionID)
	revoker, capped := r.revokers[sessionID]
	if !capped {
		return
	}
	delete(r.revokers, sessionID)
	mine := withoutID(r.byRevoker[revoker], sessionID)
	if len(mine) == 0 {
		delete(r.byRevoker, revoker)
	} else {
		r.byRevoker[revoker] = mine
	}
}

// IsShareViewer reports whether p is a read-only share grant for sessionID: a
// "share:{sessionID}:..." subject holding nothing beyond the viewer role and no
// session-scoped admin grant (port of is_share_viewer). A tunnel share
// operator uses the same subject prefix but holds a scoped admin grant, so it
// is not one.
func IsShareViewer(p *Principal, sessionID string) bool {
	// A tunnel share viewer is revoked by rotating the tunnel's tokens; the
	// viewer cutoff would also refuse the links a rotation hands out.
	if p == nil || p.TunnelShare || p.AdminSessionScope != nil || !strings.HasPrefix(p.SubjectID, "share:"+sessionID+":") {
		return false
	}
	for role := range p.Roles {
		if role != "viewer" {
			return false
		}
	}
	return true
}

// issuedAt returns the token's numeric iat claim.
func issuedAt(p *Principal) (float64, bool) {
	switch v := p.Claims["iat"].(type) {
	case float64:
		return v, true
	case json.Number:
		f, err := v.Float64()
		return f, err == nil
	}
	return 0, false
}

// RevokeShareViewers refuses every share-viewer token for sessionID issued at
// or before now (wall-clock seconds, the clock a token's iat is written in) and
// returns the cutoff. A token issued in the same second is refused too: iat has
// one-second resolution, so it cannot say which side of the cutoff it fell on.
// The cutoff counts against no principal; see RevokeShareViewersBy.
func (s *AuthorizationService) RevokeShareViewers(sessionID string, now float64) (float64, error) {
	return s.RevokeShareViewersBy(sessionID, now, "")
}

// RevokeShareViewersBy is RevokeShareViewers with the cutoff counted against
// revoker, who holds at most ShareViewerRevocationsPerPrincipalMax of them,
// its oldest evicted first. An empty revoker (a global admin) is not capped.
func (s *AuthorizationService) RevokeShareViewersBy(sessionID string, now float64, revoker string) (float64, error) {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cutoffs == nil {
		r.cutoffs = map[string]float64{}
		r.revokers = map[string]string{}
		r.byRevoker = map[string][]string{}
	}
	// Order is revocation order, so the stale cutoffs are at the front.
	for len(r.order) > 0 && r.cutoffs[r.order[0]] < now-ShareViewerRevocationTTLSeconds {
		r.dropLocked(r.order[0])
	}
	_, seen := r.cutoffs[sessionID]
	r.dropLocked(sessionID)
	if mine := r.byRevoker[revoker]; revoker != "" && len(mine) >= ShareViewerRevocationsPerPrincipalMax {
		r.dropLocked(mine[0])
	}
	// Nothing was dropped above unless this session or the revoker's own
	// oldest made room, so a refusal leaves the table as it was.
	if !seen && len(r.order) >= ShareViewerRevocationsMax {
		return 0, ErrShareViewerRevocationsFull
	}
	r.cutoffs[sessionID] = now
	r.order = append(r.order, sessionID)
	if revoker != "" {
		r.revokers[sessionID] = revoker
		r.byRevoker[revoker] = append(r.byRevoker[revoker], sessionID)
	}
	return now, nil
}

// ForgetShareViewers drops sessionID's cutoff: the session was deleted.
func (s *AuthorizationService) ForgetShareViewers(sessionID string) {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	r.dropLocked(sessionID)
}

// ShareViewerRevocation returns the cutoff recorded for sessionID, if any.
func (s *AuthorizationService) ShareViewerRevocation(sessionID string) (float64, bool) {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	cutoff, ok := r.cutoffs[sessionID]
	return cutoff, ok
}

// ShareViewerRevoked reports whether p is a share viewer of sessionID whose
// token predates the session's cutoff. A token without a numeric iat cannot
// show it postdates the cutoff, so it is refused.
func (s *AuthorizationService) ShareViewerRevoked(p *Principal, sessionID string) bool {
	cutoff, ok := s.ShareViewerRevocation(sessionID)
	if !ok || !IsShareViewer(p, sessionID) {
		return false
	}
	issued, ok := issuedAt(p)
	return !ok || issued <= cutoff
}
