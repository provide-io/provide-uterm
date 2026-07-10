//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// registerApprovalRoutes wires the /api/approvals approve/reject routes. Port of
// approvals.py.
//
// Deviations: GET /api/approvals (list pending) is NOT registered — the ported
// hub.InMemoryApprovalStore exposes no iterator over its internal request map,
// and this package may not modify the hub. Likewise the command-injection side
// effect of approval (Python hub.resolve_approval, which un-holds the buffered
// command and broadcasts approval_resolved) has no exported Go facade, so
// approve/reject here perform the one-shot state transition (Claim) and return
// the same REST contract, but do not re-inject the held command. Both are
// tracked as follow-ups requiring a hub API addition.
func (s *Server) registerApprovalRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/approvals/{request_id}/approve", s.authenticated(s.handleApprove))
	mux.HandleFunc("POST /api/approvals/{request_id}/reject", s.authenticated(s.handleReject))
}

// requireApprovalAdmin enforces the admin gate for approvals, returning the
// principal. It writes the (capitalized) approvals-specific 403 on failure.
func (s *Server) requireApprovalAdmin(w http.ResponseWriter, r *http.Request) bool {
	if !s.deps.Authz.IsAdmin(principalOf(r)) {
		detailError(w, http.StatusForbidden, "Admin role required")
		return false
	}
	return true
}

func (s *Server) handleApprove(w http.ResponseWriter, r *http.Request) {
	if !s.requireApprovalAdmin(w, r) {
		return
	}
	requestID := r.PathValue("request_id")
	req := s.deps.Hub.Approvals.Get(requestID)
	if req == nil {
		detailError(w, http.StatusNotFound, "Approval request not found")
		return
	}
	if req.SubmitterID == principalOf(r).SubjectID {
		detailError(w, http.StatusForbidden, "Cannot approve your own command")
		return
	}
	if !s.deps.Hub.Approvals.Claim(requestID, hub.ApprovalApproved) {
		detailError(w, http.StatusBadRequest, "Approval request is not pending")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "approved"})
}

func (s *Server) handleReject(w http.ResponseWriter, r *http.Request) {
	if !s.requireApprovalAdmin(w, r) {
		return
	}
	requestID := r.PathValue("request_id")
	req := s.deps.Hub.Approvals.Get(requestID)
	if req == nil {
		detailError(w, http.StatusNotFound, "Approval request not found")
		return
	}
	if !s.deps.Hub.Approvals.Claim(requestID, hub.ApprovalRejected) {
		detailError(w, http.StatusBadRequest, "Approval request is not pending")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "rejected"})
}
