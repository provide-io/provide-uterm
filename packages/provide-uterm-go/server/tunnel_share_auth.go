//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"regexp"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
)

// shareSessionPatterns are the request paths a tunnel-share cookie may
// authenticate, each capturing the session_id. Port of _SHARE_SESSION_PATTERNS
// (app/factory_impl.py): the session REST surface, the four session-scoped app
// pages, the browser terminal WebSocket, and the worker hijack path. The /app
// prefix is literal here exactly as in Python — a non-default UI.AppPath is not
// share-eligible in either implementation. Any non-matching path yields no
// session id, so share resolution is skipped and the configured IdP runs.
var shareSessionPatterns = []*regexp.Regexp{
	regexp.MustCompile(`^/api/sessions/(?P<session_id>[\w\-]+)(?:/.*)?$`),
	regexp.MustCompile(`^/app/(?:session|operator|replay|inspect)/(?P<session_id>[\w\-]+)$`),
	regexp.MustCompile(`^/ws/browser/(?P<session_id>[\w\-]+)/term$`),
	regexp.MustCompile(`^/worker/(?P<session_id>[\w\-]+)/hijack(?:/.*)?$`),
}

// shareSessionIDFor returns the session id a share-eligible path maps to, or ""
// when the path is not share-eligible. Port of share_session_id_for.
func shareSessionIDFor(path string) string {
	for _, pat := range shareSessionPatterns {
		m := pat.FindStringSubmatch(path)
		if m == nil {
			continue
		}
		for i, name := range pat.SubexpNames() {
			if name == "session_id" {
				return m[i]
			}
		}
	}
	return ""
}

// resolveShareprincipal resolves a viewer/operator tunnel-share principal from
// the per-session cookie, or nil when the request is not an authenticated
// share. Port of resolve_tunnel_share_principal (app/factory_tunnel_auth.py):
// share auth is cookie-only after the one-time ?invite= bootstrap — a raw token
// in the query string is never accepted (URLs leak into proxy/browser logs).
// The returned principal's subject id encodes the role (share:{id}:operator |
// share:{id}:viewer); an operator's admin grant is confined to its own session
// via AdminSessionScope so it cannot escalate to other sessions.
func (s *Server) resolveShareprincipal(r *http.Request) *serverauth.Principal {
	sessionID := shareSessionIDFor(r.URL.Path)
	if sessionID == "" {
		return nil
	}
	entry, ok := s.deps.TunnelStore.GetToken(sessionID)
	if !ok {
		return nil
	}
	cookie, err := r.Cookie("uterm_tunnel_" + sessionID)
	if err != nil {
		return nil
	}
	provided := strings.TrimSpace(cookie.Value)
	if provided == "" {
		return nil
	}
	// Expiry: expires_at is unix seconds, same units as the server clock. A
	// zero/unset expiry is treated as already-expired (fail closed, matching the
	// Python guard) — every real tunnel token carries a future expiry.
	if s.clock.Wall() > entry.ExpiresAt {
		s.logger.Info("tunnel_token_expired", "session_id", sessionID)
		return nil
	}
	src := sourceIP(r)
	// IP binding: reject when the request IP differs from the issuing IP.
	if s.cfg.Tunnel.IPBinding && entry.IssuedIP != nil && *entry.IssuedIP != "" && *entry.IssuedIP != src {
		s.logger.Info("tunnel_token_ip_mismatch", "session_id", sessionID, "issued", *entry.IssuedIP, "actual", src)
		return nil
	}
	// The stored values are BLAKE2b digests; VerifyToken hashes the supplied
	// token and constant-time compares. Control token → operator, share → viewer.
	if tunnel.VerifyToken(provided, entry.ControlTokenHash) {
		s.logger.Info("tunnel_token_validated", "session_id", sessionID, "token_type", "control", "source_ip", src)
		scope := sessionID
		return &serverauth.Principal{
			SubjectID:         "share:" + sessionID + ":operator",
			Roles:             serverauth.NewSet("admin"),
			Scopes:            serverauth.NewSet("*"),
			AdminSessionScope: &scope,
			Claims:            map[string]any{},
		}
	}
	if tunnel.VerifyToken(provided, entry.ShareTokenHash) {
		s.logger.Info("tunnel_token_validated", "session_id", sessionID, "token_type", "share", "source_ip", src)
		return &serverauth.Principal{
			SubjectID: "share:" + sessionID + ":viewer",
			Roles:     serverauth.NewSet("viewer"),
			Scopes:    serverauth.NewSet("session.read"),
			Claims:    map[string]any{},
		}
	}
	s.logger.Info("tunnel_token_validation_failed", "session_id", sessionID, "source_ip", src)
	return nil
}

// shareRoleOf returns the tunnel-share role ("operator" or "viewer") a share
// principal carries in its subject id (share:{session_id}:{role}), or "" for any
// non-share principal. It is the page-bootstrap analogue of Python's
// request.state.uterm_share_role.
func shareRoleOf(p *serverauth.Principal) string {
	if p == nil || !strings.HasPrefix(p.SubjectID, "share:") {
		return ""
	}
	if i := strings.LastIndex(p.SubjectID, ":"); i >= 0 && i+1 < len(p.SubjectID) {
		return p.SubjectID[i+1:]
	}
	return ""
}

// sharePageRole returns the share role for a page bootstrap as an any: a
// non-empty role string, or nil so the "share_role" key serializes to JSON null
// (matching Python's None default for non-share principals).
func sharePageRole(p *serverauth.Principal) any {
	if role := shareRoleOf(p); role != "" {
		return role
	}
	return nil
}
