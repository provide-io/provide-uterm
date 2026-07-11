//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
)

// registerTunnelLifecycleRoutes wires the full tunnel invite/token surface.
// Ports the routes in routes/tunnels.py (create/rotate/revoke) plus the
// anonymous /s/{id} share consumer from app/routes_wiring.py.
//
// Route parity vs Python:
//
//	POST   /api/tunnels                          create_tunnel        (auth)
//	DELETE /api/tunnels/{tunnel_id}/tokens        revoke_tunnel_tokens (auth)
//	POST   /api/tunnels/{tunnel_id}/tokens/rotate rotate_tunnel_tokens (auth)
//	GET    /s/{session_id}                        short_share_url      (anon)
//
// GET /api/tunnels has NO Python equivalent — it is a Go-only owner/admin
// listing of tunnel metadata (never tokens). See handleListTunnels.
func (s *Server) registerTunnelLifecycleRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/tunnels", s.authenticated(s.handleCreateTunnel))
	mux.HandleFunc("GET /api/tunnels", s.authenticated(s.handleListTunnels))
	mux.HandleFunc("DELETE /api/tunnels/{tunnel_id}/tokens", s.authenticated(s.handleRevokeTunnelTokens))
	mux.HandleFunc("POST /api/tunnels/{tunnel_id}/tokens/rotate", s.authenticated(s.handleRotateTunnelTokens))
	// The share consumer is intentionally anonymous: it exchanges a one-time
	// ?invite= for an HttpOnly cookie, then redirects. Auth happens on the
	// subsequent cookie-bearing request (see the Python /s/{id} route).
	mux.HandleFunc("GET /s/{session_id}", s.handleShareConsumer)
}

// canManageTunnel reports whether the principal may revoke/rotate a tunnel's
// tokens: an admin, or the owner of the tunnel session. Mirrors the Python
// (is_admin or is_owner) guard.
func (s *Server) canManageTunnel(p *serverauth.Principal, def *serverconfig.SessionDefinition) bool {
	return s.deps.Authz.IsAdmin(p) || s.deps.Authz.IsOwner(p, def)
}

// tunnelSharePage maps a tunnel type to its default share landing page.
func tunnelSharePage(tunnelType string) string {
	if tunnelType == "http" {
		return "inspect"
	}
	return "session"
}

// wsBase converts an http(s) base URL to its ws(s) form.
func wsBaseURL(base string) string {
	base = strings.Replace(base, "http://", "ws://", 1)
	return strings.Replace(base, "https://", "wss://", 1)
}

// requestBaseURL derives scheme://host from the request, used only as a
// fallback when Config.Server.PublicBaseURL is empty (it is normally derived to
// a non-empty value at config load).
func requestBaseURL(r *http.Request) string {
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	return scheme + "://" + r.Host
}

// tunnelBaseURL returns the public base URL (PublicBaseURL, else request-derived).
func (s *Server) tunnelBaseURL(r *http.Request) string {
	if s.cfg.Server.PublicBaseURL != "" {
		return strings.TrimRight(s.cfg.Server.PublicBaseURL, "/")
	}
	return requestBaseURL(r)
}

// handleCreateTunnel ports create_tunnel: mints a tunnel session + worker/share/
// control tokens (stored hashed) + one-time invites, returning share/control
// URLs and the worker token.
func (s *Server) handleCreateTunnel(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	if !s.deps.Authz.CanCreateSession(p) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, _ := decodeJSONBody(r)
	tunnelType := strDefault(strings.TrimSpace(stringField(body, "tunnel_type")), "terminal")
	displayName := strDefault(strings.TrimSpace(stringField(body, "display_name")), "tunnel")
	tunnelID := "tunnel-" + randHex(12)

	workerToken := tunnel.GenerateToken()
	shareToken := tunnel.GenerateToken()
	controlToken := tunnel.GenerateToken()

	tunnelCfg := s.cfg.Tunnel
	// TTL: per-tunnel override clamped to [60, server default * 24].
	requestedTTL := tunnelCfg.TokenTTLS
	if v, ok := floatField(body, "ttl_s"); ok {
		requestedTTL = int(v)
	}
	ttlS := clampInt(requestedTTL, 60, tunnelCfg.TokenTTLS*24)
	now := s.clock.Wall()
	expiresAt := now + float64(ttlS)
	srcIP := sourceIP(r)

	payload := map[string]any{
		"session_id":       tunnelID,
		"display_name":     displayName,
		"connector_type":   "websocket",
		"connector_config": map[string]any{"tunnel_type": tunnelType},
		"input_mode":       "open",
		"auto_start":       false,
		"ephemeral":        true,
		// Tunnels rely on one-time invite URLs that bootstrap an HttpOnly cookie;
		// the session itself is private and owned by the creator.
		"owner":             p.SubjectID,
		"visibility":        "private",
		"recording_enabled": true,
	}
	// Internal create: the tunnel session is an inbound placeholder (websocket
	// connector, tunnel_type, no dial-out url) so it bypasses the connector-
	// target egress check, mirroring create_session(validate_connector_target=
	// False). The payload is server-built, not caller-controlled.
	if _, err := s.deps.Registry.CreateSessionInternal(r.Context(), payload); err != nil {
		s.writeCreateError(w, err)
		return
	}

	var issuedIP *string
	if tunnelCfg.IPBinding {
		ip := srcIP
		issuedIP = &ip
	}
	s.deps.TunnelStore.PutToken(tunnelID, tunnel.TokenRecord{
		WorkerTokenHash:  tunnel.HashToken(workerToken),
		ShareTokenHash:   tunnel.HashToken(shareToken),
		ControlTokenHash: tunnel.HashToken(controlToken),
		CreatedAt:        now,
		ExpiresAt:        expiresAt,
		IssuedIP:         issuedIP,
		TunnelType:       tunnelType,
		SharePage:        tunnelSharePage(tunnelType),
	})
	shareInvite, controlInvite := s.deps.TunnelStore.IssueInvites(
		tunnelID, shareToken, controlToken, expiresAt, now, issuedIP,
	)

	s.audit(r, "tunnel.create", map[string]any{"tunnel_type": tunnelType, "ttl_s": ttlS})
	base := s.tunnelBaseURL(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"tunnel_id":    tunnelID,
		"display_name": displayName,
		"tunnel_type":  tunnelType,
		"ws_endpoint":  wsBaseURL(base) + "/tunnel/" + tunnelID,
		"worker_token": workerToken,
		"share_url":    base + "/s/" + tunnelID + "?invite=" + shareInvite,
		"control_url":  base + "/s/" + tunnelID + "?invite=" + controlInvite,
		"expires_at":   expiresAt,
	})
}

// handleRevokeTunnelTokens ports revoke_tunnel_tokens: owner/admin (when the
// session still exists) drops all tokens + invites. Idempotent — an unknown
// tunnel returns 200.
func (s *Server) handleRevokeTunnelTokens(w http.ResponseWriter, r *http.Request) {
	tunnelID := r.PathValue("tunnel_id")
	p := principalOf(r)
	def, ok := s.deps.Registry.GetDefinition(r.Context(), tunnelID)
	if ok && !s.canManageTunnel(p, def) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	s.deps.TunnelStore.DeleteToken(tunnelID)
	s.deps.TunnelStore.DiscardInvitesForSession(tunnelID)
	s.audit(r, "tunnel.tokens.revoke", map[string]any{"session_id": tunnelID})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "session_id": tunnelID})
}

// handleRotateTunnelTokens ports rotate_tunnel_tokens: owner/admin mints fresh
// tokens + invites for an existing tunnel.
func (s *Server) handleRotateTunnelTokens(w http.ResponseWriter, r *http.Request) {
	tunnelID := r.PathValue("tunnel_id")
	p := principalOf(r)
	def, ok := s.deps.Registry.GetDefinition(r.Context(), tunnelID)
	if !ok {
		detailError(w, http.StatusNotFound, "unknown session: "+tunnelID)
		return
	}
	if !s.canManageTunnel(p, def) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	old, ok := s.deps.TunnelStore.GetToken(tunnelID)
	if !ok {
		detailError(w, http.StatusNotFound, "no tunnel tokens for "+tunnelID)
		return
	}

	tunnelCfg := s.cfg.Tunnel
	ttlS := tunnelCfg.TokenTTLS
	now := s.clock.Wall()
	expiresAt := now + float64(ttlS)
	workerToken := tunnel.GenerateToken()
	shareToken := tunnel.GenerateToken()
	controlToken := tunnel.GenerateToken()
	srcIP := sourceIP(r)

	tunnelType := strDefault(old.TunnelType, "terminal")
	var issuedIP *string
	if tunnelCfg.IPBinding {
		ip := srcIP
		issuedIP = &ip
	}
	s.deps.TunnelStore.PutToken(tunnelID, tunnel.TokenRecord{
		WorkerTokenHash:  tunnel.HashToken(workerToken),
		ShareTokenHash:   tunnel.HashToken(shareToken),
		ControlTokenHash: tunnel.HashToken(controlToken),
		CreatedAt:        now,
		ExpiresAt:        expiresAt,
		IssuedIP:         issuedIP,
		TunnelType:       tunnelType,
		SharePage:        tunnelSharePage(tunnelType),
	})
	s.deps.TunnelStore.DiscardInvitesForSession(tunnelID)
	shareInvite, controlInvite := s.deps.TunnelStore.IssueInvites(
		tunnelID, shareToken, controlToken, expiresAt, now, issuedIP,
	)

	s.audit(r, "tunnel.tokens.rotate", map[string]any{"session_id": tunnelID})
	base := s.tunnelBaseURL(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"tunnel_id":    tunnelID,
		"ws_endpoint":  wsBaseURL(base) + "/tunnel/" + tunnelID,
		"worker_token": workerToken,
		"share_url":    base + "/s/" + tunnelID + "?invite=" + shareInvite,
		"control_url":  base + "/s/" + tunnelID + "?invite=" + controlInvite,
		"expires_at":   expiresAt,
	})
}

// handleListTunnels lists tunnel metadata (never tokens) for the caller's
// tunnels; admins see all. This has NO Python parity route — it is a Go-only
// convenience over the token store, gated by owner/admin like the mutate routes.
func (s *Server) handleListTunnels(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	isAdmin := s.deps.Authz.IsAdmin(p)
	out := make([]map[string]any, 0)
	for id, rec := range s.deps.TunnelStore.ListTokens() {
		if !isAdmin {
			def, ok := s.deps.Registry.GetDefinition(r.Context(), id)
			if !ok || !s.deps.Authz.IsOwner(p, def) {
				continue
			}
		}
		out = append(out, map[string]any{
			"tunnel_id":   id,
			"tunnel_type": rec.TunnelType,
			"share_page":  rec.SharePage,
			"created_at":  rec.CreatedAt,
			"expires_at":  rec.ExpiresAt,
		})
	}
	writeJSON(w, http.StatusOK, out)
}

// handleShareConsumer ports short_share_url (/s/{id}?invite=...): consumes a
// one-time invite, sets the HttpOnly per-session tunnel cookie, and 302s to the
// share/operator page. It is anonymous.
func (s *Server) handleShareConsumer(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	entry, _ := s.deps.TunnelStore.GetToken(sessionID)
	inviteValue := r.URL.Query().Get("invite")

	var invite *tunnel.Invite
	if inviteValue != "" {
		invite = s.deps.TunnelStore.ConsumeInvite(inviteValue, sessionID, s.clock.Wall())
		if invite == nil {
			detailError(w, http.StatusForbidden, "invalid or expired invite")
			return
		}
		tokenHash := entry.ShareTokenHash
		if invite.Role == tunnel.RoleOperator {
			tokenHash = entry.ControlTokenHash
		}
		if !tunnel.InviteMatchesTokenHash(invite, tokenHash) {
			detailError(w, http.StatusForbidden, "stale invite")
			return
		}
	}

	page := strDefault(entry.SharePage, "session")
	if invite != nil && invite.Role == tunnel.RoleOperator {
		page = "operator"
	}
	target := s.cfg.UI.AppPath + "/" + page + "/" + sessionID
	if invite != nil {
		// The Secure flag comes from static config, never from a spoofable
		// forwarded-proto header, so an untrusted peer cannot flip it.
		http.SetCookie(w, &http.Cookie{
			Name:  "uterm_tunnel_" + sessionID,
			Value: invite.TunnelToken,
			// Path "/" so the cookie reaches /app/{page}/{id} and the session
			// API/WS (Go would otherwise scope it to /s/; Starlette's set_cookie
			// defaults path="/").
			Path:     "/",
			Secure:   s.cfg.Tunnel.CookieSecure,
			HttpOnly: true,
			SameSite: sameSiteMode(s.cfg.Tunnel.CookieSamesite),
		})
	}
	http.Redirect(w, r, target, http.StatusFound)
}

// sameSiteMode maps the config samesite string to the http.SameSite enum.
func sameSiteMode(v string) http.SameSite {
	switch strings.ToLower(v) {
	case "strict":
		return http.SameSiteStrictMode
	case "none":
		return http.SameSiteNoneMode
	default:
		return http.SameSiteLaxMode
	}
}
