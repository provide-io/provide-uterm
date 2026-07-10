//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// allowUnauthEnvVar is the explicit opt-out that re-enables the legacy
// warn-and-skip behaviour on a non-loopback bind.
const allowUnauthEnvVar = "UTERM_MANAGER_ALLOW_UNAUTHENTICATED"

// loopbackHosts are treated as loopback for the unauthenticated-dev fallback.
// 0.0.0.0 is intentionally NOT loopback.
var loopbackHosts = map[string]struct{}{
	"127.0.0.1": {},
	"localhost": {},
	"::1":       {},
}

// selfReportRoute pairs a method with a fully-anchored path pattern capturing
// the agent_id. Mirrors manager/auth.py _WORKER_SELF_REPORT_ROUTES.
type selfReportRoute struct {
	method  string
	pattern *regexp.Regexp
}

var workerSelfReportRoutes = []selfReportRoute{
	{"POST", regexp.MustCompile(`^/agent/([^/]+)/status$`)},
	{"POST", regexp.MustCompile(`^/agent/([^/]+)/register$`)},
}

// deriveAgentToken derives the per-agent worker token bound to agentID:
// "sha256=" + HMAC-SHA256(secret, agentID). Mirrors manager/auth.py.
func deriveAgentToken(secret, agentID string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(agentID))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// extractSelfReportAgentID returns the agent_id for a worker-self-report
// (method, path), or "" and false otherwise.
func extractSelfReportAgentID(path, method string) (string, bool) {
	for _, r := range workerSelfReportRoutes {
		if method != r.method {
			continue
		}
		if m := r.pattern.FindStringSubmatch(path); m != nil {
			return m[1], true
		}
	}
	return "", false
}

// AuthMiddleware enforces a bearer token exactly like TokenAuthMiddleware in
// manager/auth.py. It wraps an http.Handler.
type AuthMiddleware struct {
	token           string
	workerToken     *string
	workerSecret    *string
	enforcePerAgent bool
	publicPaths     map[string]struct{}
	publicPrefixes  []string
	logger          *slog.Logger
}

func (m *AuthMiddleware) isPublicPath(path string) bool {
	if _, ok := m.publicPaths[path]; ok {
		return true
	}
	for _, p := range m.publicPrefixes {
		if strings.HasPrefix(path, p) {
			return true
		}
	}
	return false
}

// isAuthorized returns true if provided may access (method, path).
func (m *AuthMiddleware) isAuthorized(provided, path, method string) bool {
	if subtle.ConstantTimeCompare([]byte(provided), []byte(m.token)) == 1 {
		return true
	}
	agentID, ok := extractSelfReportAgentID(path, method)
	if !ok {
		return false
	}
	if m.workerSecret != nil { // pragma: allowlist secret
		expected := deriveAgentToken(*m.workerSecret, agentID)
		if subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) == 1 {
			return true
		}
	}
	if !m.enforcePerAgent && m.workerToken != nil {
		return subtle.ConstantTimeCompare([]byte(provided), []byte(*m.workerToken)) == 1
	}
	return false
}

// isWebsocket reports whether r is a WebSocket upgrade request.
func isWebsocket(r *http.Request) bool {
	return strings.EqualFold(r.Header.Get("Upgrade"), "websocket")
}

// extractRequestToken pulls the bearer token from r. The bool return is
// pass_through=true meaning the request should bypass auth (OPTIONS).
func extractRequestToken(r *http.Request) (string, bool) {
	if isWebsocket(r) {
		return strings.TrimSpace(r.URL.Query().Get("token")), false
	}
	if r.Method == http.MethodOptions {
		return "", true
	}
	auth := r.Header.Get("Authorization")
	if strings.HasPrefix(auth, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(auth, "Bearer ")), false
	}
	return strings.TrimSpace(r.Header.Get("X-Api-Token")), false
}

// Wrap returns an http.Handler that enforces the token before delegating.
func (m *AuthMiddleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if m.isPublicPath(path) {
			next.ServeHTTP(w, r)
			return
		}
		provided, passThrough := extractRequestToken(r)
		if passThrough {
			next.ServeHTTP(w, r)
			return
		}
		if !m.isAuthorized(provided, path, r.Method) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": "Unauthorized"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// isLoopbackBind reports whether host is a loopback/local-only bind address.
func isLoopbackBind(host string) bool {
	if host == "" {
		return false
	}
	_, ok := loopbackHosts[strings.ToLower(strings.TrimSpace(host))]
	return ok
}

// truthyEnv reports whether v is a truthy opt-in string.
func truthyEnv(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes":
		return true
	}
	return false
}

// SetupAuth builds the auth middleware, mirroring manager/auth.py setup_auth.
// It returns (nil, nil) when auth is intentionally skipped (loopback/opt-out),
// (mw, nil) when configured, or (nil, err) when a non-loopback bind lacks a
// token. getenv is injectable for testing.
func SetupAuth(cfg *ManagerConfig, envVar string, getenv func(string) string) (*AuthMiddleware, error) {
	logger := ptel.GetLogger(context.Background(), "provide.uterm.manager.auth")
	if getenv == nil {
		getenv = func(string) string { return "" }
	}
	token := strings.TrimSpace(getenv(envVar))
	if token == "" {
		var bindHost string
		if cfg != nil {
			bindHost = cfg.Host
		}
		if truthyEnv(getenv(allowUnauthEnvVar)) {
			logger.Warn("api_token_auth_disabled", "hint", "Set "+envVar+" to enable", "reason", "explicit_opt_out")
			return nil, nil
		}
		if cfg == nil || isLoopbackBind(bindHost) {
			reason := "no_config"
			if cfg != nil {
				reason = "loopback_bind"
			}
			logger.Warn("api_token_auth_disabled", "hint", "Set "+envVar+" to enable", "reason", reason, "bind_host", bindHost)
			return nil, nil
		}
		return nil, fmt.Errorf( //nolint:staticcheck // matches Python error text
			"Manager API token is required when binding to a non-loopback host (%q). Set the %s environment variable, bind to 127.0.0.1/localhost/::1, or set %s=1 to explicitly run unauthenticated",
			bindHost, envVar, allowUnauthEnvVar)
	}

	publicPaths := map[string]struct{}{}
	var publicPrefixes []string
	workerEnvVar := "UTERM_MANAGER_WORKER_TOKEN"
	enforce := false
	if cfg != nil {
		for _, p := range cfg.AuthPublicPaths {
			publicPaths[p] = struct{}{}
		}
		publicPrefixes = append(publicPrefixes, cfg.AuthPublicPrefixes...)
		if cfg.AuthWorkerTokenEnvVar != "" {
			workerEnvVar = cfg.AuthWorkerTokenEnvVar
		}
		enforce = cfg.EnforcePerAgentWorkerToken
	}
	var workerToken *string
	if wt := strings.TrimSpace(getenv(workerEnvVar)); wt != "" {
		workerToken = &wt
	}
	logger.Info("api_token_auth_enabled",
		"worker_token_scoped", workerToken != nil,
		"enforce_per_agent_worker_token", enforce)
	return &AuthMiddleware{
		token:           token,
		workerToken:     workerToken,
		workerSecret:    workerToken,
		enforcePerAgent: enforce,
		publicPaths:     publicPaths,
		publicPrefixes:  publicPrefixes,
		logger:          logger,
	}, nil
}
