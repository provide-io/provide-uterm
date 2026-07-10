//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"
)

// registerTunnelRoutes wires POST /api/connect (quick-connect) plus the full
// tunnel invite/token lifecycle. Port of tunnels.py + the /s/{id} share
// consumer from app/routes_wiring.py. The lifecycle routes are registered by
// registerTunnelLifecycleRoutes (routes_tunnels_full.go), backed by the tunnel
// package (BLAKE2b token hashing, one-time invites, share cookies).
func (s *Server) registerTunnelRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/connect", s.authenticated(s.handleQuickConnect))
	s.registerTunnelLifecycleRoutes(mux)
}

// reservedConnectKeys are the top-level quick-connect keys that are NOT folded
// into connector_config.
var reservedConnectKeys = map[string]struct{}{
	"connector_type": {}, "display_name": {}, "input_mode": {}, "tags": {},
	"auto_start": {}, "visibility": {}, "owner": {}, "recording_enabled": {}, "ephemeral": {},
}

func (s *Server) handleQuickConnect(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	if !s.deps.Authz.CanCreateSession(p) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, _ := decodeJSONBody(r)
	connectorType := strDefault(strings.TrimSpace(stringField(body, "connector_type")), "ssh")
	displayName := strDefault(stringField(body, "display_name"), connectorType)
	inputMode := strDefault(stringField(body, "input_mode"), "open")
	connectorConfig := map[string]any{}
	for k, v := range body {
		if _, reserved := reservedConnectKeys[k]; !reserved {
			connectorConfig[k] = v
		}
	}
	payload := map[string]any{
		"session_id":       "connect-" + randHex(12),
		"display_name":     displayName,
		"connector_type":   connectorType,
		"connector_config": connectorConfig,
		"input_mode":       inputMode,
		"owner":            p.SubjectID,
		"visibility":       "private",
		"ephemeral":        true,
		"tags":             stringList(body["tags"]),
	}
	if boolField(body, "recording_enabled", false) {
		payload["recording_enabled"] = true
	}
	st, err := s.deps.Registry.CreateSession(r.Context(), payload)
	if err != nil {
		s.writeCreateError(w, err)
		return
	}
	s.audit(r, "session.create", map[string]any{"connector_type": connectorType, "ephemeral": true})
	writeJSON(w, http.StatusOK, s.sessionConnectResponse(st))
}
