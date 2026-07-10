//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import "net/http"

// registerWebhookRoutes wires the session-webhook routes. Port of webhooks.py.
// When no WebhookManager is configured every route returns 503 (matching the
// Python "webhook manager not available" branch).
func (s *Server) registerWebhookRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/sessions/{session_id}/webhooks", s.authenticated(s.handleRegisterWebhook))
	mux.HandleFunc("GET /api/sessions/{session_id}/webhooks", s.authenticated(s.handleListWebhooks))
	mux.HandleFunc("DELETE /api/sessions/{session_id}/webhooks/{webhook_id}", s.authenticated(s.handleUnregisterWebhook))
}

// webhookMgr returns the manager after the session-exists + mutate gate, or nil
// (having written the response) on any failure.
func (s *Server) webhookGate(w http.ResponseWriter, r *http.Request) (string, bool) {
	id, _, ok := s.gatedSession(w, r, "session.control.update")
	if !ok {
		return "", false
	}
	if s.deps.Webhooks == nil {
		detailError(w, http.StatusServiceUnavailable, "webhook manager not available")
		return "", false
	}
	return id, true
}

func (s *Server) handleRegisterWebhook(w http.ResponseWriter, r *http.Request) {
	id, ok := s.webhookGate(w, r)
	if !ok {
		return
	}
	body, _ := decodeJSONBody(r)
	url := stringField(body, "url")
	if url == "" {
		detailError(w, http.StatusUnprocessableEntity, "url is required")
		return
	}
	if err := s.deps.Webhooks.ValidateURL(url); err != nil {
		detailError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	var eventTypes []string
	if raw, present := body["event_types"]; present {
		list, isList := raw.([]any)
		if !isList {
			detailError(w, http.StatusUnprocessableEntity, "event_types must be a list")
			return
		}
		eventTypes = stringList(list)
	}
	pattern := stringField(body, "pattern")
	if pattern != "" {
		if err := s.deps.Webhooks.ValidatePattern(pattern); err != nil {
			detailError(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
	}
	cfg, err := s.deps.Webhooks.Register(id, url, eventTypes, pattern, stringField(body, "secret"))
	if err != nil {
		detailError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, cfg)
}

func (s *Server) handleListWebhooks(w http.ResponseWriter, r *http.Request) {
	id, ok := s.webhookGate(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"webhooks": s.deps.Webhooks.ListWebhooks(id)})
}

func (s *Server) handleUnregisterWebhook(w http.ResponseWriter, r *http.Request) {
	id, ok := s.webhookGate(w, r)
	if !ok {
		return
	}
	webhookID := r.PathValue("webhook_id")
	if !requireID(w, "webhook_id", webhookID) {
		return
	}
	cfg, found := s.deps.Webhooks.GetWebhook(webhookID)
	if !found || stringField(cfg, "session_id") != id {
		detailError(w, http.StatusNotFound, "unknown webhook: "+webhookID)
		return
	}
	s.deps.Webhooks.Unregister(webhookID)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "webhook_id": webhookID})
}
