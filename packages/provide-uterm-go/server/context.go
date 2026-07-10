//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

type ctxKey int

const (
	ctxKeyPrincipal ctxKey = iota
	ctxKeyRequestID
)

// withPrincipal returns a copy of ctx carrying the resolved principal. The
// Python analogue is request.state.uterm_principal.
func withPrincipal(ctx context.Context, p *serverauth.Principal) context.Context {
	return context.WithValue(ctx, ctxKeyPrincipal, p)
}

// principalOf returns the principal resolved by auth middleware, or nil when
// none was attached (the analogue of request.state.uterm_principal being None).
func principalOf(r *http.Request) *serverauth.Principal {
	p, _ := r.Context().Value(ctxKeyPrincipal).(*serverauth.Principal)
	return p
}

// sourceIP returns the direct connection IP or "unknown", matching the Python
// audit source-ip helper (request.client.host or "unknown"). It intentionally
// does NOT trust X-Forwarded-For (spoofable without a trusted-proxy allowlist).
func sourceIP(r *http.Request) string {
	host := r.RemoteAddr
	if host == "" {
		return "unknown"
	}
	// RemoteAddr is "ip:port"; strip the port.
	if idx := strings.LastIndex(host, ":"); idx != -1 {
		host = host[:idx]
	}
	if host == "" {
		return "unknown"
	}
	return host
}

// isAnonymous reports whether the principal is the anonymous fallback (or nil).
func isAnonymous(p *serverauth.Principal) bool {
	return p == nil || p.SubjectID == "anonymous"
}

// queryInt reads an integer query param with a default and inclusive clamp to
// [min,max]. An unparseable value falls back to def. This mirrors the FastAPI
// Query(default=, ge=, le=) contract closely enough for interop (FastAPI would
// 422 an out-of-range value; here we clamp, which is a documented deviation for
// the read-only list endpoints).
func queryInt(r *http.Request, name string, def, minV, maxV int) int {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return def
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	if v < minV {
		return minV
	}
	if v > maxV {
		return maxV
	}
	return v
}
