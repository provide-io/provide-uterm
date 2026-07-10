//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"net/http"
	"strconv"
)

// handleHealth serves GET /health, mirroring health_check.
func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

// handleSwarmStatus serves GET /swarm/status, mirroring status.
func (s *Server) handleSwarmStatus(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.GetSwarmStatus())
}

// handleTimeseriesInfo serves GET /swarm/timeseries/info.
func (s *Server) handleTimeseriesInfo(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.GetTimeseriesInfo())
}

// handleTimeseriesRecent serves GET /swarm/timeseries/recent.
func (s *Server) handleTimeseriesRecent(w http.ResponseWriter, r *http.Request) {
	limit := 200
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			limit = n
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"rows": s.M.GetTimeseriesRecent(limit),
		"info": s.M.GetTimeseriesInfo(),
	})
}

// handleTimeseriesSummary serves GET /swarm/timeseries/summary.
func (s *Server) handleTimeseriesSummary(w http.ResponseWriter, r *http.Request) {
	windowMinutes := 120
	if v := r.URL.Query().Get("window_minutes"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			windowMinutes = n
		}
	}
	writeJSON(w, http.StatusOK, s.M.GetTimeseriesSummary(windowMinutes))
}
