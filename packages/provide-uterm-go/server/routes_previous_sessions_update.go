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
	"strings"
	"unicode/utf8"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// PATCH /api/previous-sessions/{session_id}: set an ended session's owner
// and/or visibility. Port of update_previous_session.

const (
	updateNothingElse = "a previous session takes owner and/or visibility, nothing else"
	updateAdminOnly   = "admin privileges required"
	updateNotSaved    = "previous session not saved"
)

// previousChanges is a PATCH body once it is known to hold only an owner and
// a visibility the live session would take; nil leaves a field as it is.
type previousChanges struct {
	owner, visibility *string
}

// decodePreviousBody reads the body as one JSON object, numbers kept as
// written so a refusal can quote them. ok is false for anything else: no
// body, malformed JSON, bytes that are not UTF-8 (the decoder would swap each
// for U+FFFD and go on, where the reference refuses them), JSON that is not
// an object, or anything after the object. net/http never hands a handler a
// nil body.
func decodePreviousBody(r *http.Request) (map[string]any, bool) {
	data, err := io.ReadAll(r.Body)
	if err != nil || !utf8.Valid(data) {
		return nil, false
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var body map[string]any
	if err := dec.Decode(&body); err != nil || body == nil {
		return nil, false
	}
	_, err = dec.Token()
	return body, errors.Is(err, io.EOF)
}

// previousChangesOf judges body for the session id, with the message of the
// refusal when it is not acceptable. The visibility is judged as the live
// session's definition judges it, and in the reference's words, so a previous
// session takes exactly what a live one does. The owner must be a string,
// though: one with none counts against the PAM user it names, which is where
// it started, so there is nothing to set it back to.
func previousChangesOf(id string, body map[string]any) (previousChanges, string) {
	var changes previousChanges
	if len(body) == 0 {
		return changes, updateNothingElse
	}
	for key := range body {
		if key != "owner" && key != "visibility" {
			return changes, updateNothingElse
		}
	}
	if raw, present := body["owner"]; present {
		owner, ok := raw.(string)
		if !ok {
			return changes, "owner must be a string"
		}
		changes.owner = &owner
	}
	if raw, present := body["visibility"]; present {
		visibility, _ := raw.(string)
		if visibility != "public" && visibility != "operator" && visibility != "private" {
			return changes, "invalid visibility for " + id + ": " + pythonRepr(raw)
		}
		changes.visibility = &visibility
	}
	return changes, ""
}

// mayUpdatePrevious reports whether p may set an ended session's owner and
// visibility: an admin in the listing's sense, globally (callerStanding: the
// provider's IsAdmin with the take-over capability), so a token whose scopes
// would keep it from reading the session cannot make itself its owner and
// then read it; and allowed the live PATCH by CanMutateSession with
// session.control.update, as the live owner change is. Never a share viewer.
func (s *Server) mayUpdatePrevious(p *serverauth.Principal, kept *capturestate.Record) bool {
	if strings.HasPrefix(p.SubjectID, "share:") || !s.callerStanding(p).admin {
		return false
	}
	def := kept.Definition
	return s.deps.Authz.CanMutateSession(p, &def, "session.control.update")
}

func (s *Server) handleUpdatePreviousSession(w http.ResponseWriter, r *http.Request) {
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
	kept, listedNow := store.PreviousSummary(id)
	if !listedNow {
		detailError(w, http.StatusNotFound, unknown)
		return
	}
	// Before the body is judged: a caller learns nothing about its handling
	// that it could not learn without one.
	p := principalOf(r)
	if !s.mayUpdatePrevious(p, kept) {
		detailError(w, http.StatusForbidden, updateAdminOnly)
		return
	}
	body, ok := decodePreviousBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	changes, refusal := previousChangesOf(id, body)
	if refusal != "" {
		detailError(w, http.StatusUnprocessableEntity, refusal)
		return
	}
	rec, err := store.UpdatePrevious(id, changes.owner, changes.visibility)
	if errors.Is(err, capturestate.ErrNotSaved) {
		// The store has logged why; the old file and the listing are unchanged.
		detailError(w, http.StatusInternalServerError, updateNotSaved)
		return
	}
	if rec == nil {
		detailError(w, http.StatusNotFound, unknown)
		return
	}
	detail := map[string]any{"session_id": id}
	for key, value := range body {
		detail[key] = value
	}
	s.audit(r, "previous_session.update", detail)
	writeJSON(w, http.StatusOK, rec.Summary())
}

// pythonRepr is how the reference's repr() writes a decoded JSON value, for
// the words of a refusal that quotes one.
func pythonRepr(value any) string {
	switch v := value.(type) {
	case nil:
		return "None"
	case bool:
		if v {
			return "True"
		}
		return "False"
	case json.Number:
		return v.String()
	case string:
		return pythonStringRepr(v)
	default:
		out, _ := json.Marshal(v)
		return string(bytes.TrimSpace(out))
	}
}

// pythonStringRepr quotes s as Python does: single quotes, unless it holds a
// single quote and no double quote.
func pythonStringRepr(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, c := range s {
		switch {
		case c == quote || c == '\\':
			b.WriteByte('\\')
			b.WriteRune(c)
		case c == '\n':
			b.WriteString(`\n`)
		case c == '\r':
			b.WriteString(`\r`)
		case c == '\t':
			b.WriteString(`\t`)
		case c < 0x20 || c == 0x7f:
			b.WriteString(`\x`)
			b.WriteByte("0123456789abcdef"[c>>4])
			b.WriteByte("0123456789abcdef"[c&15])
		default:
			b.WriteRune(c)
		}
	}
	b.WriteRune(quote)
	return b.String()
}
