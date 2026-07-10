//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"errors"
	"net/http"
	"os"
	"strings"
)

// Server wires an AgentManager to an http.ServeMux, mirroring the FastAPI app
// created by create_manager_app.
type Server struct {
	M      *AgentManager
	getenv func(string) string
}

// AppOptions configures CreateManagerApp.
type AppOptions struct {
	// WorkerRegistry maps worker types to their spawn entries.
	WorkerRegistry map[string]WorkerRegistryEntry
	// TimeseriesPlugin customizes timeseries rows/summaries (nil = bare).
	TimeseriesPlugin TimeseriesPlugin
	// Getenv overrides environment lookups (nil = os.Getenv). Used for auth,
	// CORS, and config-dir resolution.
	Getenv func(string) string
	// Now overrides the wall clock (nil = real time).
	Now func() float64
}

// CreateManagerApp builds a wired manager + HTTP handler, mirroring
// create_manager_app. It returns the Server, the fully-wrapped handler
// (CORS → auth → routes), and any configuration error.
func CreateManagerApp(cfg ManagerConfig, opts AppOptions) (*Server, http.Handler, error) {
	getenv := opts.Getenv
	if getenv == nil {
		getenv = os.Getenv
	}
	m := NewAgentManager(cfg, opts.TimeseriesPlugin, opts.Now)
	pm := NewAgentProcessManager(m, opts.WorkerRegistry, cfg.LogDir)
	pm.getenv = getenv
	if cfg.SpawnPolicyWebhookURL != "" {
		pm.SetPolicyGate(NewWebhookAgentSpawnPolicyGate(cfg.SpawnPolicyWebhookURL, cfg.SpawnPolicyWebhookSecret, cfg.SpawnPolicyWebhookTimeoutS))
	}
	m.PM = pm

	auth, err := SetupAuth(&cfg, cfg.AuthTokenEnvVar, getenv)
	if err != nil {
		return nil, nil, err
	}

	// CORS — credentials are always allowed, so an empty/wildcard allowlist
	// would expose the API to credentialed cross-site requests. Refuse both.
	origins := cfg.CORSOrigins
	if corsEnv := strings.TrimSpace(getenv("UTERM_CORS_ORIGINS")); corsEnv != "" {
		origins = nil
		for _, o := range strings.Split(corsEnv, ",") {
			if t := strings.TrimSpace(o); t != "" {
				origins = append(origins, t)
			}
		}
	}
	if len(origins) == 0 {
		return nil, nil, errors.New("CORS origin allowlist is empty; credentialed CORS requires an explicit list of origins (set UTERM_CORS_ORIGINS or ManagerConfig.cors_origins)")
	}
	for _, o := range origins {
		if o == "*" {
			return nil, nil, errors.New("CORS wildcard origin '*' is not allowed when credentials are enabled; list explicit origins instead")
		}
	}

	s := &Server{M: m, getenv: getenv}
	mux := s.Routes()

	var handler http.Handler = mux
	if auth != nil {
		handler = auth.Wrap(handler)
	}
	handler = corsMiddleware(origins, handler)

	m.LoadState()
	return s, handler, nil
}

// Routes registers every manager REST route on a fresh mux.
func (s *Server) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	// Health + swarm control (spawn.py).
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("POST /swarm/spawn", s.handleSpawn)
	mux.HandleFunc("POST /swarm/spawn-batch", s.handleSpawnBatch)
	mux.HandleFunc("POST /swarm/desired", s.handleSetDesired)
	mux.HandleFunc("POST /swarm/bust-respawn", s.handleBustRespawn)
	mux.HandleFunc("POST /swarm/kill-all", s.handleKillAll)
	mux.HandleFunc("POST /swarm/clear", s.handleClear)
	mux.HandleFunc("POST /swarm/prune", s.handlePrune)
	mux.HandleFunc("POST /swarm/pause", s.handlePauseSwarm)
	mux.HandleFunc("POST /swarm/resume", s.handleResumeSwarm)
	mux.HandleFunc("POST /agent/{agent_id}/pause", s.handlePauseAgent)
	mux.HandleFunc("POST /agent/{agent_id}/resume", s.handleResumeAgent)
	mux.HandleFunc("POST /agent/{agent_id}/restart", s.handleRestartAgent)
	// Status + timeseries (status.py).
	mux.HandleFunc("GET /swarm/status", s.handleSwarmStatus)
	mux.HandleFunc("GET /swarm/timeseries/info", s.handleTimeseriesInfo)
	mux.HandleFunc("GET /swarm/timeseries/recent", s.handleTimeseriesRecent)
	mux.HandleFunc("GET /swarm/timeseries/summary", s.handleTimeseriesSummary)
	// Per-agent ops (agent_ops.py).
	mux.HandleFunc("GET /agents", s.handleListAgents)
	mux.HandleFunc("GET /agent/{agent_id}/status", s.handleAgentStatus)
	mux.HandleFunc("GET /agent/{agent_id}/details", s.handleAgentDetails)
	mux.HandleFunc("GET /agent/{agent_id}/session-data", s.handleAgentSessionData)
	mux.HandleFunc("POST /agent/{agent_id}/register", s.handleRegisterAgent)
	mux.HandleFunc("POST /agent/{agent_id}/set-goal", s.handleSetGoal)
	mux.HandleFunc("POST /agent/{agent_id}/set-directive", s.handleSetDirective)
	mux.HandleFunc("POST /agent/{agent_id}/cancel-command", s.handleCancelCommand)
	mux.HandleFunc("DELETE /agent/{agent_id}", s.handleDeleteAgent)
	mux.HandleFunc("GET /agent/{agent_id}/events", s.handleAgentEvents)
	// Worker status self-report (agent_update.py).
	mux.HandleFunc("POST /agent/{agent_id}/status", s.handleUpdateStatus)
	return mux
}

// corsMiddleware applies a credentialed CORS policy for the given allowlist.
func corsMiddleware(origins []string, next http.Handler) http.Handler {
	allow := map[string]struct{}{}
	for _, o := range origins {
		allow[o] = struct{}{}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if _, ok := allow[origin]; ok && origin != "" {
			h := w.Header()
			h.Set("Access-Control-Allow-Origin", origin)
			h.Set("Access-Control-Allow-Credentials", "true")
			h.Set("Vary", "Origin")
			if r.Method == http.MethodOptions {
				h.Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
				h.Set("Access-Control-Allow-Headers", "*")
			}
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}
