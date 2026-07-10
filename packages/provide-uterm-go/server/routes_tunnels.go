//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"
)

// registerTunnelRoutes wires POST /api/connect (quick-connect). Port of the
// quick_connect route in tunnels.py.
//
// Deviation: the tunnel token/invite lifecycle routes (POST /api/tunnels,
// DELETE/POST /api/tunnels/{id}/tokens[/rotate], and the /s/{id} share consumer)
// are NOT ported — they depend on the tunnel-invite issuance/verification
// infrastructure (issue_tunnel_invites, BLAKE2b token hashing, share cookies)
// which is not part of the ported Go package set. quick_connect is the only
// tunnel-family route the Go client targets.
func (s *Server) registerTunnelRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/connect", s.authenticated(s.handleQuickConnect))
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
