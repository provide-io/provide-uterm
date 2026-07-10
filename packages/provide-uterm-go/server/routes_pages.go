//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"html"
	"net/http"
	"strings"
)

// registerPageRoutes wires the HTML dashboard pages (under Config.UI.AppPath)
// and the static asset mount (Config.UI.AssetsPath). Port of pages.py +
// mount_frontend_assets.
//
// Deviation: the Python pages render full xterm.js shells from server/ui.py and
// set auth/tunnel cookies. This port serves a minimal HTML shell that points at
// the mounted assets; the auth-cookie side effect is omitted (the SPA reads its
// token from the standard auth flow).
func (s *Server) registerPageRoutes(mux *http.ServeMux) {
	app := strings.TrimRight(s.cfg.UI.AppPath, "/")
	if app == "" {
		app = "/app"
	}
	mux.HandleFunc("GET "+app+"/{$}", s.authenticated(s.handleDashboardPage))
	mux.HandleFunc("GET "+app+"/connect", s.authenticated(s.handleConnectPage))
	mux.HandleFunc("GET "+app+"/session/{session_id}", s.authenticated(s.handleSessionPage))
	mux.HandleFunc("GET "+app+"/operator/{session_id}", s.authenticated(s.handleSessionPage))
	mux.HandleFunc("GET "+app+"/replay/{session_id}", s.authenticated(s.handleSessionPage))
	mux.HandleFunc("GET "+app+"/inspect/{session_id}", s.authenticated(s.handleSessionPage))

	if s.deps.FrontendDir != "" {
		assets := strings.TrimRight(s.cfg.UI.AssetsPath, "/")
		if assets == "" {
			assets = "/_terminal"
		}
		fs := http.StripPrefix(assets+"/", http.FileServer(http.Dir(s.deps.FrontendDir)))
		mux.Handle("GET "+assets+"/", fs)
	}
}

func (s *Server) handleDashboardPage(w http.ResponseWriter, _ *http.Request) {
	s.writeHTML(w, s.cfg.Server.Title, "operator dashboard")
}

func (s *Server) handleConnectPage(w http.ResponseWriter, _ *http.Request) {
	s.writeHTML(w, s.cfg.Server.Title, "connect")
}

func (s *Server) handleSessionPage(w http.ResponseWriter, r *http.Request) {
	id, ok := s.readableSession(w, r)
	if !ok {
		return
	}
	s.writeHTML(w, s.cfg.Server.Title, "session "+id)
}

// writeHTML renders the minimal page shell.
func (s *Server) writeHTML(w http.ResponseWriter, title, heading string) {
	assets := html.EscapeString(s.cfg.UI.AssetsPath)
	body := "<!-- provide-uterm shell -->\n" +
		"<main data-assets=\"" + assets + "\">\n" +
		"  <h1>" + html.EscapeString(title) + "</h1>\n" +
		"  <p>" + html.EscapeString(heading) + "</p>\n" +
		"</main>\n"
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(body))
}
