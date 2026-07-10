//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"errors"
	"net/http"
	"sort"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// registerSessionRoutes wires the /api/sessions routes. Port of sessions.py.
func (s *Server) registerSessionRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/sessions", s.authenticated(s.handleListSessions))
	mux.HandleFunc("DELETE /api/sessions", s.authenticated(s.handleBulkDeleteSessions))
	mux.HandleFunc("POST /api/sessions", s.authenticated(s.handleCreateSession))
	mux.HandleFunc("GET /api/sessions/{session_id}", s.authenticated(s.handleGetSession))
	mux.HandleFunc("PATCH /api/sessions/{session_id}", s.authenticated(s.handlePatchSession))
	mux.HandleFunc("DELETE /api/sessions/{session_id}", s.authenticated(s.handleDeleteSession))
	s.registerSessionControlRoutes(mux)
}

// definitionOr404 loads a session definition, writing 404 when unknown.
func (s *Server) definitionOr404(w http.ResponseWriter, r *http.Request, id string) (*serverconfig.SessionDefinition, bool) {
	def, ok := s.deps.Registry.GetDefinition(r.Context(), id)
	if !ok {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return nil, false
	}
	return def, true
}

// handleListSessions lists readable sessions with query filtering + paging.
func (s *Server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	q := r.URL.Query()
	items := s.deps.Registry.ListWithDefinitions(r.Context())
	out := make([]*SessionStatus, 0, len(items))
	for _, it := range items {
		if it.Status == nil || it.Definition == nil {
			continue
		}
		if !s.deps.Authz.CanReadSession(p, it.Definition) {
			continue
		}
		out = append(out, it.Status)
	}
	out = filterSessions(out, q)
	sortSessions(out, q.Get("sort"), q.Get("order"))
	limit := queryInt(r, "limit", 50, 1, 200)
	offset := queryInt(r, "offset", 0, 0, 1<<30)
	out = pageSessions(out, offset, limit)
	writeJSON(w, http.StatusOK, out)
}

// filterSessions applies the tag/connector_type/visibility/state/q filters.
func filterSessions(in []*SessionStatus, q map[string][]string) []*SessionStatus {
	tags := q["tag"]
	connector := first(q, "connector_type")
	visibility := first(q, "visibility")
	state := first(q, "state")
	search := strings.ToLower(first(q, "q"))
	out := in[:0:0]
	for _, st := range in {
		if len(tags) > 0 && !hasAllTags(st.Tags, tags) {
			continue
		}
		if connector != "" && st.ConnectorType != connector {
			continue
		}
		if visibility != "" && st.Visibility != visibility {
			continue
		}
		if state != "" && st.LifecycleState != state {
			continue
		}
		if search != "" && !sessionMatchesSearch(st, search) {
			continue
		}
		out = append(out, st)
	}
	return out
}

func first(q map[string][]string, k string) string {
	if v := q[k]; len(v) > 0 {
		return v[0]
	}
	return ""
}

func hasAllTags(have []string, want []string) bool {
	set := make(map[string]struct{}, len(have))
	for _, t := range have {
		set[t] = struct{}{}
	}
	for _, t := range want {
		if _, ok := set[t]; !ok {
			return false
		}
	}
	return true
}

func sessionMatchesSearch(st *SessionStatus, needle string) bool {
	if strings.Contains(strings.ToLower(st.SessionID), needle) ||
		strings.Contains(strings.ToLower(st.DisplayName), needle) {
		return true
	}
	for _, t := range st.Tags {
		if strings.Contains(strings.ToLower(t), needle) {
			return true
		}
	}
	return false
}

// sortSessions sorts by an allow-listed key (default created_at), descending
// unless order=="asc".
func sortSessions(in []*SessionStatus, key, order string) {
	switch key {
	case "created_at", "display_name", "session_id":
	default:
		key = "created_at"
	}
	asc := order == "asc"
	sort.SliceStable(in, func(i, j int) bool {
		var a, b string
		switch key {
		case "display_name":
			a, b = in[i].DisplayName, in[j].DisplayName
		case "session_id":
			a, b = in[i].SessionID, in[j].SessionID
		default:
			a, b = in[i].CreatedAt, in[j].CreatedAt
		}
		if asc {
			return a < b
		}
		return a > b
	})
}

func pageSessions(in []*SessionStatus, offset, limit int) []*SessionStatus {
	if offset >= len(in) {
		return []*SessionStatus{}
	}
	end := offset + limit
	if end > len(in) {
		end = len(in)
	}
	return in[offset:end]
}

// handleGetSession returns one session's status.
func (s *Server) handleGetSession(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return
	}
	if !s.deps.Authz.CanReadSession(principalOf(r), def) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	st, err := s.deps.Registry.GetSession(r.Context(), id)
	if err != nil {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

// handleCreateSession creates a session, enforcing owner scoping for non-admins.
func (s *Server) handleCreateSession(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	if !s.deps.Authz.CanCreateSession(p) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, ok := decodeJSONBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	if !s.deps.Authz.IsAdmin(p) {
		owner, hasOwner := body["owner"]
		if hasOwner && owner != nil && owner != p.SubjectID {
			detailError(w, http.StatusForbidden, "owner must match authenticated subject")
			return
		}
		body["owner"] = p.SubjectID
	}
	st, err := s.deps.Registry.CreateSession(r.Context(), body)
	if err != nil {
		s.writeCreateError(w, err)
		return
	}
	s.audit(r, "session.create", map[string]any{"session_id": st.SessionID})
	writeJSON(w, http.StatusOK, st)
}

// writeCreateError maps a create/connect registry error to its status code.
func (s *Server) writeCreateError(w http.ResponseWriter, err error) {
	var ve *SessionValidationError
	var ce *SessionConflictError
	var ee *EgressBlockedError
	switch {
	case errors.As(err, &ve):
		detailError(w, http.StatusUnprocessableEntity, ve.Msg)
	case errors.As(err, &ee):
		detailError(w, http.StatusUnprocessableEntity, ee.Msg)
	case errors.As(err, &ce):
		detailError(w, http.StatusConflict, ce.Msg)
	default:
		detailError(w, http.StatusInternalServerError, err.Error())
	}
}

// handlePatchSession applies a partial update.
func (s *Server) handlePatchSession(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return
	}
	if !s.deps.Authz.CanMutateSession(principalOf(r), def, "session.control.update") {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, ok := decodeJSONBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	st, err := s.deps.Registry.UpdateSession(r.Context(), id, body)
	if err != nil {
		var ve *SessionValidationError
		if errors.As(err, &ve) {
			detailError(w, http.StatusUnprocessableEntity, ve.Msg)
			return
		}
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

// handleDeleteSession deletes a session (idempotent) and returns {"ok":true}.
func (s *Server) handleDeleteSession(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return
	}
	if !s.deps.Authz.CanMutateSession(principalOf(r), def, "session.control.delete") {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	_ = s.deps.Registry.DeleteSession(r.Context(), id)
	s.audit(r, "session.delete", map[string]any{"session_id": id})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// handleBulkDeleteSessions deletes sessions matching a state/age filter (admin).
func (s *Server) handleBulkDeleteSessions(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	if !s.deps.Authz.IsAdmin(p) {
		detailError(w, http.StatusForbidden, "admin privileges required for bulk delete")
		return
	}
	body, ok := decodeJSONBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	filter, _ := body["filter"].(map[string]any)
	stateFilter := ""
	if sv, ok := filter["state"].(string); ok {
		stateFilter = strings.TrimSpace(sv)
	}
	olderThan, hasOlder := floatField(filter, "older_than_s")
	now := s.clock.Wall()
	deleted := 0
	for _, it := range s.deps.Registry.ListWithDefinitions(r.Context()) {
		if it.Status == nil || it.Definition == nil {
			continue
		}
		if !s.deps.Authz.CanMutateSession(p, it.Definition, "session.control.delete") {
			continue
		}
		if stateFilter != "" && it.Status.LifecycleState != stateFilter {
			continue
		}
		if hasOlder {
			if it.Status.StoppedAt == nil || now-*it.Status.StoppedAt < olderThan {
				continue
			}
		}
		_ = s.deps.Registry.DeleteSession(r.Context(), it.Status.SessionID)
		deleted++
	}
	if deleted > 0 {
		s.audit(r, "session.bulk_delete", map[string]any{"deleted": deleted})
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": deleted})
}

func floatField(m map[string]any, k string) (float64, bool) {
	if m == nil {
		return 0, false
	}
	switch v := m[k].(type) {
	case float64:
		return v, true
	case int:
		return float64(v), true
	default:
		return 0, false
	}
}

// audit emits a structured audit log line (the Go analogue of audit_event).
func (s *Server) audit(r *http.Request, action string, detail map[string]any) {
	p := principalOf(r)
	subject := ""
	if p != nil {
		subject = p.SubjectID
	}
	s.logger.Info("audit_event", "action", action, "principal", subject,
		"source_ip", sourceIP(r), "detail", detail)
}
