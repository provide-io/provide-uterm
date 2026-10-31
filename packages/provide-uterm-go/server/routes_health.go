//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"math"
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// serverFeatures is the optional behaviour a client cannot otherwise tell apart
// from an older server, listed in a ready /api/health answer (port of
// SERVER_FEATURES). "viewers.revoke.subject": the viewer-revoke route honours a
// {"subject": ...} body; a server without it ignores the body and revokes
// every share viewer of the session, so a caller must not send one blind.
var serverFeatures = []string{"viewers.revoke.subject"}

// The features listed only when configured, since a server can have either
// switched off. "sessions.capture.persist": PAM capture sessions are
// re-created under their ids after a restart
// (session_persistence.persist_capture). "sessions.previous": an ended
// capture session can be read at GET /api/previous-sessions
// (session_persistence.previous_keep_days). "sessions.previous.owner": an
// admin can set an ended capture session's owner and visibility
// (PATCH /api/previous-sessions/{id}), listed with "sessions.previous" since
// it has nothing to change without it. Port of server_features.
const (
	persistCaptureFeature   = "sessions.capture.persist"
	previousSessionsFeature = "sessions.previous"
	previousOwnerFeature    = "sessions.previous.owner"
)

// captureSocketModeFeature: the sockets this server binds for pty_capture
// sessions let the socket directory's group connect, not only this server's
// user (pam.capture_socket_mode = 0o660). Listed only then, so a capture
// producer running as another user can tell that it will be able to connect.
// Other sockets are not covered: a pty session's injected capture socket stays
// owner-only in its own private directory. Port of CAPTURE_SOCKET_MODE_FEATURE.
const captureSocketModeFeature = "capture.socket_mode"

// features is what /api/health lists, given the config.
func (s *Server) features() []string {
	out := append([]string(nil), serverFeatures...)
	if s.cfg.SessionPersistence.PersistCapture {
		out = append(out, persistCaptureFeature)
	}
	if s.cfg.SessionPersistence.PreviousKeepDays > 0 {
		out = append(out, previousSessionsFeature, previousOwnerFeature)
	}
	if s.cfg.Pam.CaptureSocketMode == serverconfig.OwnerAndGroupCaptureSocketMode {
		out = append(out, captureSocketModeFeature)
	}
	return out
}

// registerHealthRoutes wires the anonymous liveness/readiness/health routes and
// the authenticated security-posture route. Port of create_health_router.
func (s *Server) registerHealthRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.HandleFunc("GET /readyz", s.handleReadyz)
	mux.HandleFunc("GET /api/security-posture", s.authenticated(s.handleSecurityPosture))
}

// handleHealth is the rich health endpoint. It is 503 until the server is ready,
// then 200 with version/uptime/session counts and the supported features.
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
		"features":              s.features(),
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
