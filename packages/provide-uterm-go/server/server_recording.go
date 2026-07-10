//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"path/filepath"
	"strconv"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
)

// registerRecordingRoutes wires the read-only session recording routes. Port of
// the recording endpoints in routes/sessions.py. Reads flow through the
// recording.Store in Deps.Recording (a NullStore when unconfigured), gated by
// CanReadRecording after the session-exists check.
func (s *Server) registerRecordingRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/sessions/{session_id}/recording", s.authenticated(s.handleRecordingMeta))
	mux.HandleFunc("GET /api/sessions/{session_id}/recording/entries", s.authenticated(s.handleRecordingEntries))
	mux.HandleFunc("GET /api/sessions/{session_id}/recording/download", s.authenticated(s.handleRecordingDownload))
}

// recordingGate resolves the session, enforces CanReadRecording, and returns the
// session id, or writes the error response and returns ok=false.
func (s *Server) recordingGate(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("session_id")
	if !requireID(w, "session_id", id) {
		return "", false
	}
	def, ok := s.definitionOr404(w, r, id)
	if !ok {
		return "", false
	}
	if !s.deps.Authz.CanReadRecording(principalOf(r), def) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return "", false
	}
	return id, true
}

// handleRecordingMeta returns the recording metadata (session_id/exists/size).
func (s *Server) handleRecordingMeta(w http.ResponseWriter, r *http.Request) {
	id, ok := s.recordingGate(w, r)
	if !ok {
		return
	}
	meta, err := s.deps.Recording.RecordingMeta(id)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, meta)
}

// handleRecordingEntries returns paginated recording entries. limit is clamped
// to 1..500 (default 200); offset (when present) skips from the start, else the
// tail is returned; event filters by event type.
func (s *Server) handleRecordingEntries(w http.ResponseWriter, r *http.Request) {
	id, ok := s.recordingGate(w, r)
	if !ok {
		return
	}
	q := recording.Query{Limit: queryInt(r, "limit", 200, 1, 500), Event: r.URL.Query().Get("event")}
	if raw := r.URL.Query().Get("offset"); raw != "" {
		off, err := strconv.Atoi(raw)
		if err != nil || off < 0 {
			detailError(w, http.StatusUnprocessableEntity, "offset must be a non-negative integer")
			return
		}
		q.Offset = &off
	}
	entries, err := s.deps.Recording.GetEntries(id, q)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, entries)
}

// handleRecordingDownload streams the raw JSONL recording file. Port of
// recording_download: 404 when there is no local file, and a path-confinement
// check ensures the file lives under the configured recording directory before
// it is served (defense against a store returning an out-of-tree path).
func (s *Server) handleRecordingDownload(w http.ResponseWriter, r *http.Request) {
	id, ok := s.recordingGate(w, r)
	if !ok {
		return
	}
	path, err := s.deps.Recording.GetPath(id)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if path == "" || !recordingPathAllowed(path, s.cfg.Recording.Directory) {
		detailError(w, http.StatusNotFound, "recording not available")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Disposition", "attachment; filename=\""+filepath.Base(path)+"\"")
	http.ServeFile(w, r, path)
}

// recordingPathAllowed reports whether path resolves under dir, mirroring
// Python's path.resolve().is_relative_to(directory.resolve()). Symlinks are
// resolved so a symlinked recording file cannot escape the directory.
func recordingPathAllowed(path, dir string) bool {
	if dir == "" {
		return false
	}
	resolvedPath, err := filepath.EvalSymlinks(path)
	if err != nil {
		return false
	}
	resolvedDir, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return false
	}
	absPath, err := filepath.Abs(resolvedPath)
	if err != nil {
		return false
	}
	absDir, err := filepath.Abs(resolvedDir)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(absDir, absPath)
	if err != nil {
		return false
	}
	return rel != ".." && !hasDotDotPrefix(rel)
}

// hasDotDotPrefix reports whether rel steps out of its base (starts with "..").
func hasDotDotPrefix(rel string) bool {
	return len(rel) >= 2 && rel[0] == '.' && rel[1] == '.' &&
		(len(rel) == 2 || rel[2] == filepath.Separator)
}
