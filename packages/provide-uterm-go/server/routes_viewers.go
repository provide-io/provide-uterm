//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// viewerRevokedCloseCode is the close code a revoked share viewer's socket gets:
// an application code (4000-4999) so a client can tell "access was taken away"
// from a drop or a policy refusal and stop reconnecting. 4410 echoes HTTP 410.
const viewerRevokedCloseCode = 4410

// viewerRevokedReason is deliberately generic: the embedder knows why.
const viewerRevokedReason = "viewer access revoked"

// registerViewerRoutes wires POST /api/sessions/{session_id}/viewers/revoke
// (port of routes/viewers.py). Runtime-native, not a shared RouteDef.
func (s *Server) registerViewerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/sessions/{session_id}/viewers/revoke", s.authenticated(s.handleRevokeViewers))
}

// handleRevokeViewers records a share-viewer cutoff for the session and closes
// every live browser socket held by a share viewer it now refuses. Allowed to
// whoever may change the session (session.control.update).
func (s *Server) handleRevokeViewers(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.update")
	if !ok {
		return
	}
	authz := s.deps.Authz
	// The embedder's service token is a global admin and revokes for everyone,
	// so only other callers are held to a per-principal share of the table.
	revoker := ""
	if p := principalOf(r); !authz.IsAdmin(p) {
		revoker = p.SubjectID
	}
	revokedAt, err := authz.RevokeShareViewersBy(id, s.deps.Clock.Wall(), revoker)
	if err != nil {
		detailError(w, http.StatusTooManyRequests, "too many sessions with revoked viewers; try again later")
		return
	}
	// By principal alone: a provider can resolve a share viewer to a role other
	// than viewer, and what is revoked is the token, not the role.
	closed := s.deps.Hub.CloseBrowsersWhere(r.Context(), id, func(ws hub.BrowserConn, _ string) bool {
		bc, isBrowser := ws.(*browserConn)
		return isBrowser && authz.ShareViewerRevoked(bc.principal, id)
	}, viewerRevokedCloseCode, viewerRevokedReason)
	// Live event streams opened with a revoked token end too, and count.
	closed += s.viewerStreams.revoke(authz, id)
	s.audit(r, "session.viewers.revoke", map[string]any{"session_id": id, "closed": closed, "revoked_at": revokedAt})
	writeJSON(w, http.StatusOK, map[string]any{
		"session_id":   id,
		"revoked_at":   revokedAt,
		"closed":       closed,
		"close_code":   viewerRevokedCloseCode,
		"close_reason": viewerRevokedReason,
	})
}
