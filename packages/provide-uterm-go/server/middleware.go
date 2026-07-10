//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// statusRecorder captures the response status code for access logging + metrics.
type statusRecorder struct {
	http.ResponseWriter
	status  int
	written bool
}

func (rec *statusRecorder) WriteHeader(code int) {
	if !rec.written {
		rec.status = code
		rec.written = true
	}
	rec.ResponseWriter.WriteHeader(code)
}

func (rec *statusRecorder) Write(b []byte) (int, error) {
	if !rec.written {
		rec.status = http.StatusOK
		rec.written = true
	}
	return rec.ResponseWriter.Write(b)
}

// Unwrap exposes the underlying ResponseWriter so http.ResponseController (used
// by the WebSocket hijack path) can reach the connection.
func (rec *statusRecorder) Unwrap() http.ResponseWriter { return rec.ResponseWriter }

// Flush delegates to the underlying ResponseWriter so SSE handlers can stream
// (the wrapper would otherwise hide the Flusher from a type assertion).
func (rec *statusRecorder) Flush() {
	if f, ok := rec.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// newRequestID returns an X-Request-ID value (16 random bytes hex).
func newRequestID() string {
	var b [16]byte
	// rand.Read never returns an error for crypto/rand on supported platforms;
	// the value only needs to be unique, not unpredictable.
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// requestLogging is the innermost-registered / access-log middleware. It sets
// the X-Request-ID (echoing an inbound one), counts requests + 4xx/5xx, and
// logs one line per request. Port of _request_logging_middleware.
func (s *Server) requestLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqID := r.Header.Get("X-Request-ID")
		if reqID == "" {
			reqID = newRequestID()
		}
		w.Header().Set("X-Request-ID", reqID)
		ctx := context.WithValue(r.Context(), ctxKeyRequestID, reqID)
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		s.metrics.Inc("http_requests_total", 1)
		start := time.Now()
		next.ServeHTTP(rec, r.WithContext(ctx))
		switch {
		case rec.status >= 500:
			s.metrics.Inc("http_requests_5xx_total", 1)
		case rec.status >= 400:
			s.metrics.Inc("http_requests_4xx_total", 1)
		}
		s.logger.Debug("http_request",
			"method", r.Method, "path", r.URL.Path, "status", rec.status,
			"duration_ms", time.Since(start).Milliseconds(), "request_id", reqID)
	})
}

// securityHeaders applies the configured CSP/HSTS/etc. response headers. Port of
// SecurityHeadersMiddleware (dev mode strips them via ResolveSecurityHeaders).
func (s *Server) securityHeaders(next http.Handler) http.Handler {
	pairs := serverauth.ResolveSecurityHeaders(&s.cfg.Security)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for _, p := range pairs {
			w.Header().Set(p.Name, p.Value)
		}
		next.ServeHTTP(w, r)
	})
}

// isWSUpgrade reports whether r is a WebSocket upgrade request.
func isWSUpgrade(r *http.Request) bool {
	return strings.EqualFold(r.Header.Get("Upgrade"), "websocket")
}

// corsAndOrigin applies CORS headers for allowed origins and enforces the
// WebSocket same-origin / allow-list gate. Port of CORSMiddleware +
// WebSocketOriginMiddleware.
func (s *Server) corsAndOrigin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if isWSUpgrade(r) && !s.originAllowed(r) {
			// Python closes the WS with code 4403; before the upgrade the
			// closest HTTP-layer equivalent is rejecting the handshake 403,
			// which bridge.TermBridge also treats as permanent.
			detailError(w, http.StatusForbidden, "origin not allowed")
			return
		}
		if len(s.allowedOrigins) > 0 {
			s.applyCORS(w, r)
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusOK)
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

// originAllowed implements the WS origin decision: wildcard, no-Origin
// (non-browser), same-origin, or explicit allow-list membership.
func (s *Server) originAllowed(r *http.Request) bool {
	if s.originWildcard {
		return true
	}
	origin := strings.ToLower(strings.TrimRight(r.Header.Get("Origin"), "/"))
	if origin == "" {
		return true // non-browser client; auth handled elsewhere
	}
	host := strings.ToLower(strings.TrimRight(r.Host, "/"))
	proto := "http"
	if r.TLS != nil {
		proto = "https"
	}
	if origin == proto+"://"+host {
		return true
	}
	_, ok := s.allowedOrigins[origin]
	return ok
}

// applyCORS sets the CORS response headers for a matched origin.
func (s *Server) applyCORS(w http.ResponseWriter, r *http.Request) {
	origin := strings.ToLower(strings.TrimRight(r.Header.Get("Origin"), "/"))
	if origin == "" {
		return
	}
	_, listed := s.allowedOrigins[origin]
	if !listed && !s.originWildcard {
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", r.Header.Get("Origin"))
	w.Header().Set("Access-Control-Allow-Credentials", "true")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Request-ID")
	w.Header().Set("Vary", "Origin")
}

// authRequest builds the transport-agnostic serverauth.Request from an
// *http.Request.
func authRequest(r *http.Request) *serverauth.Request {
	headers := make(map[string]string, len(r.Header))
	for k, v := range r.Header {
		if len(v) > 0 {
			headers[k] = v[0]
		}
	}
	cookies := make(map[string]string)
	for _, c := range r.Cookies() {
		cookies[c.Name] = c.Value
	}
	return &serverauth.Request{Headers: headers, Cookies: cookies, SourceIP: sourceIP(r)}
}

// resolvePrincipal resolves the request principal. A tunnel-share cookie is
// tried first (port of _require_authenticated running resolve_tunnel_share_
// principal ahead of the configured resolver), then the configured
// authenticator. A nil principal (webhook deny) is normalized to the anonymous
// principal so callers only test isAnonymous.
func (s *Server) resolvePrincipal(r *http.Request) *serverauth.Principal {
	if share := s.resolveShareprincipal(r); share != nil {
		return share
	}
	p, err := s.deps.Auth.Authenticate(r.Context(), authRequest(r))
	if err != nil || p == nil {
		return serverauth.AnonymousPrincipal()
	}
	return p
}

// authenticated wraps a handler so it runs only for a non-anonymous principal,
// attaching it to the request context. Anonymous → 401 (Python
// require_authenticated). This is applied per route group, matching FastAPI's
// per-router Depends(require_authenticated).
func (s *Server) authenticated(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := s.resolvePrincipal(r)
		if isAnonymous(p) {
			s.metrics.Inc("auth_failures_http_total", 1)
			detailError(w, http.StatusUnauthorized, "authentication required")
			return
		}
		next(w, r.WithContext(withPrincipal(r.Context(), p)))
	}
}
