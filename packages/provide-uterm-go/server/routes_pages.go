//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// registerPageRoutes wires the HTML dashboard pages (under Config.UI.AppPath)
// and the static asset mount (Config.UI.AssetsPath). Port of pages.py + ui.py +
// mount_frontend_assets.
//
// The tunnel-share flow is at parity: the /s/{id} share consumer
// (handleShareConsumer in routes_tunnels_full.go) sets the HttpOnly
// uterm_tunnel_{id} cookie, resolvePrincipal validates it into a share
// principal (resolveShareprincipal in tunnel_share_auth.go), and the session/
// operator/replay/inspect handlers emit its role in the "share_role" bootstrap
// via sharePageRole. Non-share principals emit null, as before.
func (s *Server) registerPageRoutes(mux *http.ServeMux) {
	s.ui = newUIManifests(s.deps.FrontendDir)

	app := strings.TrimRight(s.cfg.UI.AppPath, "/")
	if app == "" {
		app = "/app"
	}
	mux.HandleFunc("GET "+app+"/{$}", s.authenticated(s.handleDashboardPage))
	mux.HandleFunc("GET "+app+"/connect", s.authenticated(s.handleConnectPage))
	mux.HandleFunc("GET "+app+"/session/{session_id}", s.authenticated(s.handleSessionPage))
	mux.HandleFunc("GET "+app+"/operator/{session_id}", s.authenticated(s.handleOperatorPage))
	mux.HandleFunc("GET "+app+"/replay/{session_id}", s.authenticated(s.handleReplayPage))
	mux.HandleFunc("GET "+app+"/inspect/{session_id}", s.authenticated(s.handleInspectPage))

	if s.deps.FrontendDir != "" {
		assets := strings.TrimRight(s.cfg.UI.AssetsPath, "/")
		if assets == "" {
			assets = "/_terminal"
		}
		fs := http.StripPrefix(assets+"/", http.FileServer(http.Dir(s.deps.FrontendDir)))
		mux.Handle("GET "+assets+"/", fs)
	}
}

// isSecureRequest reports whether the request arrived over HTTPS. Port of
// _is_secure_request: trust X-Forwarded-Proto (behind a reverse proxy) then fall
// back to the direct connection scheme.
func isSecureRequest(r *http.Request) bool {
	if strings.Contains(strings.ToLower(r.Header.Get("X-Forwarded-Proto")), "https") {
		return true
	}
	return r.TLS != nil
}

// setAuthCookie sets an HttpOnly page cookie. Port of _set_auth_cookie. Path is
// "/" so the cookie reaches every route (Go's http.SetCookie would otherwise
// default it to the request-URI directory, e.g. /app; Starlette's set_cookie
// defaults path="/", which this matches).
func setAuthCookie(w http.ResponseWriter, name, value string, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     name,
		Value:    value,
		Path:     "/",
		Secure:   secure,
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
	})
}

// setPageCookies sets the principal + surface cookies always, and the token
// cookie only in jwt mode for a non-anonymous principal. Port of
// _set_page_cookies (minus the tunnel share cookie — see registerPageRoutes).
func (s *Server) setPageCookies(w http.ResponseWriter, r *http.Request, principalName, surface string, secure bool) {
	setAuthCookie(w, s.cfg.Auth.PrincipalCookie, principalName, secure)
	setAuthCookie(w, s.cfg.Auth.SurfaceCookie, surface, secure)
	if s.cfg.Auth.Mode == "jwt" && principalName != "anonymous" {
		if token := bearerToken(r); token != "" {
			setAuthCookie(w, s.cfg.Auth.TokenCookie, token, secure)
		}
	}
}

// writePage writes an HTML page response with the standard content type.
func writePage(w http.ResponseWriter, htmlDoc string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(htmlDoc))
}

// readablePageSession validates the session_id, loads the definition (404), and
// checks the read capability (403), returning the definition + id on success.
func (s *Server) readablePageSession(w http.ResponseWriter, r *http.Request) (*serverconfig.SessionDefinition, string, bool) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return nil, "", false
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return nil, "", false
	}
	if !s.deps.Authz.CanReadSession(principalOf(r), def) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return nil, "", false
	}
	return def, id, true
}

func (s *Server) handleDashboardPage(w http.ResponseWriter, r *http.Request) {
	cdn := cdnFromUI(s.cfg.UI)
	// Cookies are response headers — set them before writing the body/status.
	s.setPageCookies(w, r, principalOf(r).Name(), "operator", isSecureRequest(r))
	writePage(w, s.ui.operatorDashboardHTML(s.cfg.Server.Title, s.cfg.UI.AppPath, s.cfg.UI.AssetsPath, cdn))
}

func (s *Server) handleConnectPage(w http.ResponseWriter, r *http.Request) {
	cdn := cdnFromUI(s.cfg.UI)
	s.setPageCookies(w, r, principalOf(r).Name(), "operator", isSecureRequest(r))
	writePage(w, s.ui.connectPageHTML(s.cfg.Server.Title, s.cfg.UI.AssetsPath, s.cfg.UI.AppPath, cdn))
}

func (s *Server) handleSessionPage(w http.ResponseWriter, r *http.Request) {
	def, id, ok := s.readablePageSession(w, r)
	if !ok {
		return
	}
	cdn := cdnFromUI(s.cfg.UI)
	s.setPageCookies(w, r, principalOf(r).Name(), "user", isSecureRequest(r))
	writePage(w, s.ui.sessionPageHTML(def.DisplayName, s.cfg.UI.AssetsPath, id, false, s.cfg.UI.AppPath, sharePageRole(principalOf(r)), cdn))
}

func (s *Server) handleOperatorPage(w http.ResponseWriter, r *http.Request) {
	def, id, ok := s.readablePageSession(w, r)
	if !ok {
		return
	}
	cdn := cdnFromUI(s.cfg.UI)
	s.setPageCookies(w, r, principalOf(r).Name(), "operator", isSecureRequest(r))
	writePage(w, s.ui.sessionPageHTML(def.DisplayName, s.cfg.UI.AssetsPath, id, true, s.cfg.UI.AppPath, sharePageRole(principalOf(r)), cdn))
}

func (s *Server) handleReplayPage(w http.ResponseWriter, r *http.Request) {
	def, id, ok := s.readablePageSession(w, r)
	if !ok {
		return
	}
	cdn := cdnFromUI(s.cfg.UI)
	s.setPageCookies(w, r, principalOf(r).Name(), "operator", isSecureRequest(r))
	writePage(w, s.ui.replayPageHTML(def.DisplayName, s.cfg.UI.AssetsPath, id, s.cfg.UI.AppPath, sharePageRole(principalOf(r)), cdn))
}

func (s *Server) handleInspectPage(w http.ResponseWriter, r *http.Request) {
	def, id, ok := s.readablePageSession(w, r)
	if !ok {
		return
	}
	cdn := cdnFromUI(s.cfg.UI)
	s.setPageCookies(w, r, principalOf(r).Name(), "operator", isSecureRequest(r))
	writePage(w, s.ui.inspectPageHTML(def.DisplayName, s.cfg.UI.AssetsPath, id, s.cfg.UI.AppPath, sharePageRole(principalOf(r)), cdn))
}
