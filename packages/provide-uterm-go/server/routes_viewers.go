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
	revokedAt := authz.RevokeShareViewers(id, s.deps.Clock.Wall())
	closed := s.deps.Hub.CloseBrowsersWhere(r.Context(), id, func(ws hub.BrowserConn, role string) bool {
		bc, isBrowser := ws.(*browserConn)
		return isBrowser && role == "viewer" && authz.ShareViewerRevoked(bc.principal, id)
	}, viewerRevokedCloseCode, viewerRevokedReason)
	s.audit(r, "session.viewers.revoke", map[string]any{"session_id": id, "closed": closed, "revoked_at": revokedAt})
	writeJSON(w, http.StatusOK, map[string]any{
		"session_id":   id,
		"revoked_at":   revokedAt,
		"closed":       closed,
		"close_code":   viewerRevokedCloseCode,
		"close_reason": viewerRevokedReason,
	})
}
