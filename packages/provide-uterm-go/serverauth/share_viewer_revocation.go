//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"encoding/json"
	"slices"
	"strings"
	"sync"
)

// ShareViewerRevocationsMax bounds how many per-session revocation cutoffs are
// held at once (port of SHARE_VIEWER_REVOCATIONS_MAX). Past it the oldest is
// forgotten, which can only matter for a share token that outlived thousands of
// later revocations.
const ShareViewerRevocationsMax = 10000

// ShareViewerRevocationTTLSeconds is how long a cutoff is kept (port of
// SHARE_VIEWER_REVOCATION_TTL_S). A cutoff only matters while a token issued
// before it can still be valid; ageing out rather than only evicting by count
// means nobody can push another session's fresh cutoff out of the table.
const ShareViewerRevocationTTLSeconds = 86400.0

// shareViewerRevocations is the per-session cutoff table behind
// AuthorizationService. The zero value is ready to use.
type shareViewerRevocations struct {
	mu      sync.Mutex
	cutoffs map[string]float64
	order   []string // oldest first
}

// IsShareViewer reports whether p is a read-only share grant for sessionID: a
// "share:{sessionID}:..." subject holding nothing beyond the viewer role and no
// session-scoped admin grant (port of is_share_viewer). A tunnel share
// operator uses the same subject prefix but holds a scoped admin grant, so it
// is not one.
func IsShareViewer(p *Principal, sessionID string) bool {
	if p == nil || p.AdminSessionScope != nil || !strings.HasPrefix(p.SubjectID, "share:"+sessionID+":") {
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
func (s *AuthorizationService) RevokeShareViewers(sessionID string, now float64) float64 {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cutoffs == nil {
		r.cutoffs = map[string]float64{}
	}
	if _, seen := r.cutoffs[sessionID]; seen {
		r.order = slices.DeleteFunc(r.order, func(id string) bool { return id == sessionID })
	}
	r.cutoffs[sessionID] = now
	r.order = append(r.order, sessionID)
	// Order is revocation order, so the stale cutoffs are at the front.
	for r.cutoffs[r.order[0]] < now-ShareViewerRevocationTTLSeconds {
		delete(r.cutoffs, r.order[0])
		r.order = r.order[1:]
	}
	for len(r.order) > ShareViewerRevocationsMax {
		delete(r.cutoffs, r.order[0])
		r.order = r.order[1:]
	}
	return now
}

// ForgetShareViewers drops sessionID's cutoff: the session was deleted.
func (s *AuthorizationService) ForgetShareViewers(sessionID string) {
	r := &s.revocations
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, seen := r.cutoffs[sessionID]; seen {
		delete(r.cutoffs, sessionID)
		r.order = slices.DeleteFunc(r.order, func(id string) bool { return id == sessionID })
	}
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
