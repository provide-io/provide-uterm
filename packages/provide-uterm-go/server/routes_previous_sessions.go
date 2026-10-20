//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// Read the capture sessions that have ended
// (session_persistence.previous_keep_days). Port of
// provide.uterm.server.routes.previous_sessions.
//
//	GET /api/previous-sessions               the previous sessions the caller may read, newest first
//	GET /api/previous-sessions/{session_id}  one of them, with its last screen and scrollback
//
// A previous session is history, so it is read by fewer people than the live
// session was: its owner, its admin, and an operator unless it was private. A
// share viewer never, whatever its token: a watch grant is for the live
// stream, and the cutoffs that revoke one are not kept across a restart.
// Everyone else is answered as the live read answers a caller it refuses: an
// id nobody kept is 404, and one the caller may not read is 403. What is
// returned passes through the output-redaction policy, as the live screen
// does.

func (s *Server) registerPreviousSessionRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/previous-sessions", s.authenticated(s.handleListPreviousSessions))
	mux.HandleFunc("GET /api/previous-sessions/{session_id}", s.authenticated(s.handleGetPreviousSession))
}

// previousStore is the store behind these routes, or nil when previous
// sessions are off.
func (s *Server) previousStore() *capturestate.Store {
	if store := s.deps.CaptureState; store != nil && store.KeepsPrevious() {
		return store
	}
	return nil
}

// standing is what the caller is, whatever the session: asked of the
// authorization provider once a request, not once a record, since under a
// webhook provider each call is a round trip. Port of Standing.
type standing struct {
	// admin is an administrator who may take over a session
	// (session.control.hijack), globally.
	admin bool
	// adminOf is the one session a session-scoped admin grant names, if any.
	adminOf  string
	operator bool
}

// callerStanding asks the provider for p's standing: admin is its IsAdmin
// (or, for a session-scoped grant, that session) with the take-over
// capability the live read's admin role requires; operator is the operator
// role, as the live read's role resolution reads it. Port of caller_standing.
func (s *Server) callerStanding(p *serverauth.Principal) standing {
	canTakeOver := s.deps.Authz.HasCapability(p, "session.control.hijack")
	st := standing{admin: canTakeOver && s.deps.Authz.IsAdmin(p), operator: p.Roles.Has("operator")}
	if canTakeOver && p.Roles.Has("admin") && p.AdminSessionScope != nil {
		st.adminOf = *p.AdminSessionScope
	}
	return st
}

// mayReadPrevious reports whether p may read an ended session: its owner,
// its admin, or an operator of one not private. Never a share viewer
// ("share:...", a watch or a tunnel share), and never anyone the live read
// would have refused: CanReadSession and the owner check are the live read's
// per-session gate, and the rest is st (callerStanding). Port of
// may_read_previous.
func (s *Server) mayReadPrevious(p *serverauth.Principal, st standing, rec *capturestate.Record) bool {
	def := rec.Definition
	if strings.HasPrefix(p.SubjectID, "share:") || !s.deps.Authz.CanReadSession(p, &def) {
		return false
	}
	if s.deps.Authz.IsOwner(p, &def) {
		return true
	}
	if st.admin || (st.adminOf != "" && st.adminOf == def.SessionID) {
		return true
	}
	return st.operator && def.Visibility != "private"
}

func (s *Server) handleListPreviousSessions(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	out := []capturestate.Summary{}
	if store := s.previousStore(); store != nil {
		kept := store.PreviousSessions()
		var st standing
		if len(kept) > 0 {
			st = s.callerStanding(p)
		}
		for _, rec := range kept {
			if s.mayReadPrevious(p, st, rec) {
				out = append(out, rec.Summary())
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": out})
}

// previousSession is one previous session as GET /api/previous-sessions/{id}
// answers it: the listing's fields, then the screen and scrollback.
type previousSession struct {
	capturestate.Summary
	Screen     string   `json:"screen"`
	Scrollback []string `json:"scrollback"`
}

func (s *Server) handleGetPreviousSession(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return
	}
	unknown := "unknown previous session: " + id
	store := s.previousStore()
	if store == nil {
		detailError(w, http.StatusNotFound, unknown)
		return
	}
	kept, ok := store.PreviousSummary(id)
	if !ok {
		detailError(w, http.StatusNotFound, unknown)
		return
	}
	// Decided on the listing's copy, before the file with the screen is read.
	p := principalOf(r)
	if !s.mayReadPrevious(p, s.callerStanding(p), kept) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	// Not found when the file has gone or turned unreadable since it was listed.
	rec, ok := store.PreviousSession(id)
	if !ok {
		detailError(w, http.StatusNotFound, unknown)
		return
	}
	// A previous record always has a screen, and a scrollback list (ParseBytes
	// refuses one without).
	texts, err := s.deps.Hub.RedactTextsForRecipient(
		r.Context(), id, append([]string{*rec.Screen}, rec.Scrollback...), &browserConn{principal: p},
	)
	if err != nil {
		// Fail closed: the unredacted screen is never the fallback.
		detailError(w, http.StatusServiceUnavailable, "output policy unavailable")
		return
	}
	writeJSON(w, http.StatusOK, previousSession{Summary: rec.Summary(), Screen: texts[0], Scrollback: texts[1:]})
}
