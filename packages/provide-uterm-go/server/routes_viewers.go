//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"unicode/utf8"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// viewerRevokedCloseCode is the close code a revoked share viewer's socket gets:
// an application code (4000-4999) so a client can tell "access was taken away"
// from a drop or a policy refusal and stop reconnecting. 4410 echoes HTTP 410.
const viewerRevokedCloseCode = 4410

// viewerRevokedReason is deliberately generic: the embedder knows why.
const viewerRevokedReason = "viewer access revoked"

// registerViewerRoutes wires POST /api/sessions/{session_id}/viewers/revoke
// (port of routes/viewers.py). Runtime-native, not a shared RouteDef.
func (s *Server) registerViewerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/sessions/{session_id}/viewers/revoke", s.authenticated(s.handleRevokeViewers))
}

// jsonWhitespace is the whitespace JSON allows around a value (RFC 8259).
const jsonWhitespace = " \t\r\n"

// errInvalidRevokeBody and errNotThisSessionsSubject are the two ways a
// revoke body is refused, each answered 422 with its own detail.
var (
	errInvalidRevokeBody      = errors.New("invalid request body")
	errNotThisSessionsSubject = errors.New("subject must be a share subject of this session")
)

// requestedSubject returns the share subject the body names, or "" to revoke
// every share viewer (a session's share subject is never empty). No body, a
// blank one, null and {} all mean the whole session, as before the body
// existed (null is "no body" across this server, as in decodeJSONBody).
// Anything else must be exactly {"subject": "<string>"}: an unknown key (a
// misspelt "subject") or a null subject is refused rather than read as
// "every viewer", which would close sockets the caller meant to leave open.
func requestedSubject(r *http.Request, sessionID string) (string, error) {
	// Read up to one byte past the cap and no further: the largest body worth
	// reading is a 512-byte subject in its envelope.
	raw, err := io.ReadAll(io.LimitReader(r.Body, serverauth.ShareViewerRevokeBodyMaxBytes+1))
	if err != nil || len(raw) > serverauth.ShareViewerRevokeBodyMaxBytes {
		return "", errInvalidRevokeBody
	}
	// JSON's own whitespace only, as every port trims, so a body of other
	// Unicode spaces is malformed everywhere rather than empty in some ports.
	raw = bytes.Trim(raw, jsonWhitespace)
	if len(raw) == 0 {
		return "", nil
	}
	// Invalid UTF-8 is refused, as the reference refuses it, rather than
	// decoded with replacement characters into a subject nobody holds.
	var fields map[string]json.RawMessage
	if !utf8.Valid(raw) || json.Unmarshal(raw, &fields) != nil {
		return "", errInvalidRevokeBody
	}
	// Only a null body decodes to a nil map ({} is an empty one).
	if fields == nil {
		return "", nil
	}
	for key := range fields {
		if key != "subject" {
			return "", errInvalidRevokeBody
		}
	}
	token, named := fields["subject"]
	if !named {
		return "", nil
	}
	// Through any, so a null subject is refused rather than read as "". The
	// error is nil: the token is a value the whole body already decoded.
	var value any
	_ = json.Unmarshal(token, &value)
	subject, isString := value.(string)
	if !isString || spellsLoneSurrogate(token) {
		return "", errInvalidRevokeBody
	}
	if !serverauth.IsShareSubjectOf(subject, sessionID) {
		return "", errNotThisSessionsSubject
	}
	return subject, nil
}

// spellsLoneSurrogate reports whether a JSON string token (quotes included)
// holds a \u escape of a surrogate that is not half of a pair. encoding/json
// decodes one to U+FFFD, a subject other than the one sent; the other ports
// refuse it as not text, and so does this one. The token has already decoded,
// so every escape in it is complete.
func spellsLoneSurrogate(token []byte) bool {
	high := false
	for i := 0; i < len(token); i++ {
		code := -1
		if token[i] == '\\' {
			i++
			if token[i] == 'u' {
				parsed, _ := strconv.ParseUint(string(token[i+1:i+5]), 16, 16)
				code = int(parsed)
				i += 4
			}
		}
		low := code >= 0xDC00 && code <= 0xDFFF
		if high != low {
			return true
		}
		high = code >= 0xD800 && code <= 0xDBFF
	}
	return false
}

// handleRevokeViewers records a share-viewer cutoff for the session, or for one
// share subject of it when the body names one, and closes every live browser
// socket held by a share viewer it now refuses. Allowed to whoever may change
// the session (session.control.update).
func (s *Server) handleRevokeViewers(w http.ResponseWriter, r *http.Request) {
	id, _, ok := s.gatedSession(w, r, "session.control.update")
	if !ok {
		return
	}
	subject, err := requestedSubject(r, id)
	if err != nil {
		detailError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	authz := s.deps.Authz
	var revokedAt float64
	if subject == "" {
		revokedAt, ok = s.revokeSession(w, r, id)
		if !ok {
			return
		}
	} else if revokedAt, err = authz.RevokeShareViewerSubject(id, subject, s.deps.Clock.Wall()); errors.Is(err, serverauth.ErrShareViewerSubjectRevocationsFull) {
		detailError(w, http.StatusTooManyRequests, "too many viewers of this session revoked one by one; revoke them all instead")
		return
	} else if err != nil {
		detailError(w, http.StatusTooManyRequests, "too many viewers revoked one by one; try again later")
		return
	}
	// By principal alone: a provider can resolve a share viewer to a role other
	// than viewer, and what is revoked is the token, not the role. The predicate
	// is the same either way: a subject cutoff refuses that subject's sockets
	// and nobody else's.
	closed := s.deps.Hub.CloseBrowsersWhere(r.Context(), id, func(ws hub.BrowserConn, _ string) bool {
		bc, isBrowser := ws.(*browserConn)
		return isBrowser && authz.ShareViewerRevoked(bc.principal, id)
	}, viewerRevokedCloseCode, viewerRevokedReason)
	// Live event streams opened with a revoked token end too, and count.
	closed += s.viewerStreams.revoke(authz, id)
	detail := map[string]any{"session_id": id, "closed": closed, "revoked_at": revokedAt}
	if subject != "" {
		detail["subject"] = subject
	}
	s.audit(r, "session.viewers.revoke", detail)
	answer := map[string]any{
		"session_id":   id,
		"revoked_at":   revokedAt,
		"closed":       closed,
		"close_code":   viewerRevokedCloseCode,
		"close_reason": viewerRevokedReason,
	}
	// The subject that was applied, so a caller can see its body was honoured.
	// Absent for a whole-session revoke, whose answer keeps its original shape.
	if subject != "" {
		answer["subject"] = subject
	}
	writeJSON(w, http.StatusOK, answer)
}

// revokeSession records the cutoff for every share viewer of the session. It
// answers the request itself and reports false when the table refuses it.
func (s *Server) revokeSession(w http.ResponseWriter, r *http.Request, id string) (float64, bool) {
	authz := s.deps.Authz
	// The embedder's service token is a global admin and revokes for everyone,
	// so only other callers are held to a per-principal share of the table.
	var revokedAt float64
	var evicted string
	var err error
	if p := principalOf(r); authz.IsAdmin(p) {
		revokedAt, err = authz.RevokeShareViewers(id, s.deps.Clock.Wall())
	} else {
		revokedAt, evicted, err = authz.RevokeShareViewersBy(id, s.deps.Clock.Wall(), p.SubjectID)
	}
	if err != nil {
		detailError(w, http.StatusTooManyRequests, "too many sessions with revoked viewers; try again later")
		return 0, false
	}
	// The cap made room by dropping this principal's oldest cutoff, so that
	// session's earlier share tokens are admitted again: say so.
	if evicted != "" {
		s.audit(r, "session.viewers.cutoff_evicted", map[string]any{"session_id": evicted, "revoked_session_id": id})
	}
	return revokedAt, true
}
