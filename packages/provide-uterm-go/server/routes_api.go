//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import "net/http"

// registerAPIRoutes wires the /api/metrics endpoints. Port of api.py.
func (s *Server) registerAPIRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/metrics", s.handleMetrics)
	mux.HandleFunc("GET /api/metrics/prometheus", s.handleMetricsPrometheus)
}

// requireMetricsAuth enforces the optional metrics auth. Returns false and
// writes a 401 when auth is required and the caller is anonymous. Port of
// _require_metrics_auth.
func (s *Server) requireMetricsAuth(w http.ResponseWriter, r *http.Request) bool {
	if !s.cfg.Security.MetricsRequireAuth {
		return true
	}
	if isAnonymous(s.resolvePrincipal(r)) {
		detailError(w, http.StatusUnauthorized, "authentication required for /metrics")
		return false
	}
	return true
}

// handleMetrics returns the JSON counter map.
func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if !s.requireMetricsAuth(w, r) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"metrics": s.metrics.Snapshot()})
}

// handleMetricsPrometheus returns the Prometheus text exposition.
func (s *Server) handleMetricsPrometheus(w http.ResponseWriter, r *http.Request) {
	if !s.requireMetricsAuth(w, r) {
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(s.metrics.Prometheus()))
}
