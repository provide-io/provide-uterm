//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// registerApprovalRoutes wires the /api/approvals list + approve/reject routes.
// Port of approvals.py. Approve/reject now resolve through hub.ResolveApproval,
// which claims the request exactly once, re-injects the held command to the
// worker (approve) or broadcasts a terminal rejection (reject), releases parked
// browsers, and broadcasts approval_resolved.
func (s *Server) registerApprovalRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/approvals", s.authenticated(s.handleListApprovals))
	mux.HandleFunc("POST /api/approvals/{request_id}/approve", s.authenticated(s.handleApprove))
	mux.HandleFunc("POST /api/approvals/{request_id}/reject", s.authenticated(s.handleReject))
}

// handleListApprovals returns every pending approval (admin only). Port of the
// GET /api/approvals list handler.
func (s *Server) handleListApprovals(w http.ResponseWriter, r *http.Request) {
	if !s.requireApprovalAdmin(w, r) {
		return
	}
	pending := s.deps.Hub.Approvals.PendingApprovals()
	out := make([]map[string]any, 0, len(pending))
	for _, req := range pending {
		out = append(out, map[string]any{
			"id":           req.ID,
			"worker_id":    req.WorkerID,
			"submitter_id": req.SubmitterID,
			"command":      req.Command,
			"status":       string(req.Status),
			"created_at":   req.CreatedAt,
			"expires_at":   req.ExpiresAt,
		})
	}
	writeJSON(w, http.StatusOK, out)
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
	resolved, err := s.deps.Hub.ResolveApproval(r.Context(), requestID, true, nil, resolverPrincipal(r))
	if err != nil {
		s.logger.Debug("resolve_approval_failed", "request_id", requestID, "error", err)
		detailError(w, http.StatusConflict, "Approval input ownership is no longer valid")
		return
	}
	if !resolved {
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
	var reason *string
	if v := r.URL.Query().Get("reason"); v != "" {
		reason = &v
	}
	resolved, err := s.deps.Hub.ResolveApproval(r.Context(), requestID, false, reason, resolverPrincipal(r))
	if err != nil {
		s.logger.Debug("resolve_approval_failed", "request_id", requestID, "error", err)
	}
	if !resolved {
		detailError(w, http.StatusBadRequest, "Approval request is not pending")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "rejected"})
}

// resolverPrincipal adapts the request principal to a hub.Principal for the
// approval audit log (nil when unauthenticated).
func resolverPrincipal(r *http.Request) *hub.Principal {
	p := principalOf(r)
	if p == nil {
		return nil
	}
	return &hub.Principal{SubjectID: p.SubjectID}
}
