//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// registerSessionControlRoutes wires the per-session control + read routes.
func (s *Server) registerSessionControlRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/sessions/{session_id}/connect", s.authenticated(s.handleConnectSession))
	mux.HandleFunc("POST /api/sessions/{session_id}/disconnect", s.authenticated(s.handleDisconnectSession))
	mux.HandleFunc("POST /api/sessions/{session_id}/restart", s.authenticated(s.handleRestartSession))
	mux.HandleFunc("POST /api/sessions/{session_id}/mode", s.authenticated(s.handleSessionMode))
	mux.HandleFunc("POST /api/sessions/{session_id}/clear", s.authenticated(s.handleClearSession))
	mux.HandleFunc("POST /api/sessions/{session_id}/annotate", s.authenticated(s.handleAnnotateSession))
	mux.HandleFunc("POST /api/sessions/{session_id}/analyze", s.authenticated(s.handleAnalyzeSession))
	mux.HandleFunc("GET /api/sessions/{session_id}/snapshot", s.authenticated(s.handleSessionSnapshot))
	mux.HandleFunc("GET /api/sessions/{session_id}/events", s.authenticated(s.handleSessionEvents))
	mux.HandleFunc("GET /api/sessions/{session_id}/events/watch", s.authenticated(s.handleWatchSessionEvents))
}

// gatedSession validates the id, loads the definition (404), and checks a
// mutate capability (403). It returns the id + definition on success.
func (s *Server) gatedSession(w http.ResponseWriter, r *http.Request, cap string) (string, *serverconfig.SessionDefinition, bool) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return "", nil, false
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return "", nil, false
	}
	if !s.deps.Authz.CanMutateSession(principalOf(r), def, cap) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return "", nil, false
	}
	return id, def, true
}

// readableSession is gatedSession's read-capability variant.
func (s *Server) readableSession(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return "", false
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return "", false
	}
	if !s.deps.Authz.CanReadSession(principalOf(r), def) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return "", false
	}
	return id, true
}

// statusOrNotFound writes st or a 404 unknown-session for a lifecycle op.
func (s *Server) statusOrNotFound(w http.ResponseWriter, id string, st *SessionStatus, err error) {
	if err != nil {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) handleConnectSession(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.connect")
	if !ok {
		return
	}
	st, err := s.deps.Registry.StartSession(r.Context(), id)
	s.statusOrNotFound(w, id, st, err)
}

func (s *Server) handleDisconnectSession(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.connect")
	if !ok {
		return
	}
	st, err := s.deps.Registry.StopSession(r.Context(), id)
	s.statusOrNotFound(w, id, st, err)
}

func (s *Server) handleRestartSession(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.connect")
	if !ok {
		return
	}
	st, err := s.deps.Registry.RestartSession(r.Context(), id)
	s.statusOrNotFound(w, id, st, err)
}

func (s *Server) handleClearSession(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.clear")
	if !ok {
		return
	}
	st, err := s.deps.Registry.ClearSession(r.Context(), id)
	s.statusOrNotFound(w, id, st, err)
}

func (s *Server) handleSessionMode(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.mode")
	if !ok {
		return
	}
	body, ok := decodeJSONBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	mode := strings.TrimSpace(stringField(body, "input_mode"))
	if mode != "open" && mode != "hijack" {
		detailError(w, http.StatusUnprocessableEntity, "input_mode must be 'open' or 'hijack'")
		return
	}
	st, err := s.deps.Registry.SetMode(r.Context(), id, mode)
	s.statusOrNotFound(w, id, st, err)
}

func (s *Server) handleAnalyzeSession(w http.ResponseWriter, r *http.Request) {
	id, ok := s.readableSession(w, r)
	if !ok {
		return
	}
	analysis, err := s.deps.Registry.AnalyzeSession(r.Context(), id)
	if err != nil {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"session_id": id, "analysis": analysis})
}

func (s *Server) handleSessionSnapshot(w http.ResponseWriter, r *http.Request) {
	id, ok := s.readableSession(w, r)
	if !ok {
		return
	}
	snap, err := s.deps.Registry.LastSnapshot(r.Context(), id)
	if err != nil {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	// snap may be nil → serializes as JSON null, matching `dict | None`.
	writeJSON(w, http.StatusOK, snap)
}

func (s *Server) handleSessionEvents(w http.ResponseWriter, r *http.Request) {
	id, ok := s.readableSession(w, r)
	if !ok {
		return
	}
	limit := queryInt(r, "limit", 100, 1, 500)
	events, err := s.deps.Registry.Events(r.Context(), id, limit)
	if err != nil {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	writeJSON(w, http.StatusOK, events)
}

func (s *Server) handleWatchSessionEvents(w http.ResponseWriter, r *http.Request) {
	id, ok := s.readableSession(w, r)
	if !ok {
		return
	}
	p := WatchParams{
		TimeoutMS: queryInt(r, "timeout_ms", 5000, 100, 30000),
		MaxEvents: queryInt(r, "max_events", 50, 1, 200),
		Pattern:   r.URL.Query().Get("pattern"),
	}
	if et := r.URL.Query().Get("event_types"); et != "" {
		p.EventTypes = splitCSV(et)
	}
	result, err := s.deps.Registry.WatchSessionEvents(r.Context(), id, p)
	if err != nil {
		detailError(w, http.StatusNotFound, "unknown session: "+id)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *Server) handleAnnotateSession(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.update")
	if !ok {
		return
	}
	body, ok := decodeJSONBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	label := strings.TrimSpace(stringField(body, "label"))
	if label == "" {
		detailError(w, http.StatusBadRequest, "label is required")
		return
	}
	severity := stringField(body, "severity")
	if severity == "" {
		severity = "info"
	}
	switch severity {
	case "info", "warning", "high", "critical":
	default:
		detailError(w, http.StatusBadRequest, "invalid severity: "+severity)
		return
	}
	ann := Annotation{
		Label:       label,
		Description: stringField(body, "description"),
		Severity:    severity,
		Principal:   principalOf(r).SubjectID,
	}
	ts, seq, err := s.deps.Registry.AnnotateSession(r.Context(), id, ann)
	if err != nil {
		detailError(w, http.StatusNotFound, "no active runtime for session: "+id)
		return
	}
	s.audit(r, "session.annotate", map[string]any{"session_id": id, "label": label})
	writeJSON(w, http.StatusOK, map[string]any{"ts": ts, "seq": seq})
}

// stringField reads a string field, tolerating a missing/non-string value.
func stringField(m map[string]any, k string) string {
	if v, ok := m[k].(string); ok {
		return v
	}
	return ""
}

// splitCSV splits a comma list, trimming and dropping empties.
func splitCSV(v string) []string {
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if t := strings.TrimSpace(p); t != "" {
			out = append(out, t)
		}
	}
	return out
}
