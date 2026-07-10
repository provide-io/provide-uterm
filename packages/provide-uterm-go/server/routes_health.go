//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"math"
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// registerHealthRoutes wires the anonymous liveness/readiness/health routes and
// the authenticated security-posture route. Port of create_health_router.
func (s *Server) registerHealthRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.HandleFunc("GET /readyz", s.handleReadyz)
	mux.HandleFunc("GET /api/security-posture", s.authenticated(s.handleSecurityPosture))
}

// handleHealth is the rich health endpoint. It is 503 until the server is ready,
// then 200 with version/uptime/session counts.
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if !s.isReady() {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"status": "starting", "ok": false, "ready": false, "service": "uterm-server",
		})
		return
	}
	backend := s.cfg.ControlPlane.Backend
	if backend == "" {
		backend = "memory"
	}
	uptime := 0.0
	if s.startTime > 0 {
		uptime = math.Round((s.clock.Wall()-s.startTime)*100) / 100
	}
	active := len(s.deps.Registry.ListWithDefinitions(r.Context()))
	writeJSON(w, http.StatusOK, map[string]any{
		"status":                "ok",
		"ok":                    true,
		"ready":                 true,
		"service":               "uterm-server",
		"version":               s.deps.Version,
		"uptime_s":              uptime,
		"active_sessions":       active,
		"control_plane_backend": backend,
	})
}

// handleHealthz is the always-200 liveness probe.
func (s *Server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

// handleReadyz reports readiness (200) or not-ready (503).
func (s *Server) handleReadyz(w http.ResponseWriter, _ *http.Request) {
	if s.isReady() {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ready"})
		return
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "not_ready"})
}

// handleSecurityPosture returns the full posture to admins/operators and a
// coarse view to other authenticated callers.
func (s *Server) handleSecurityPosture(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	secure := s.cfg.Security.Mode == "strict"
	posture := map[string]any{
		"environment": s.cfg.Environment,
		"secure":      secure,
	}
	if s.postureCallerPrivileged(p) {
		posture["mode"] = s.cfg.Security.Mode
		posture["auth_mode"] = s.cfg.Auth.Mode
		posture["metrics_require_auth"] = s.cfg.Security.MetricsRequireAuth
		posture["block_private_connector_targets"] = s.cfg.Security.BlockPrivateConnectorTargets
	}
	writeJSON(w, http.StatusOK, posture)
}

// postureCallerPrivileged reports whether the principal is admin or operator.
func (s *Server) postureCallerPrivileged(p *serverauth.Principal) bool {
	if p == nil {
		return false
	}
	return s.deps.Authz.IsAdmin(p) || s.deps.Authz.HasRole(p, "operator")
}
